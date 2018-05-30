
from tornado.gen import coroutine, Return

from common.cluster import Cluster, ClusterError
from common.database import DatabaseError, DuplicateError
from common.model import Model
from common.options import options
from common.validate import validate

from . import MessageError, MessageFlags


class GroupAdapter(object):
    def __init__(self, data):
        self.group_id = data.get("group_id")
        self.group_class = str(data.get("group_class"))
        self.key = str(data.get("group_key"))
        self.clustered = bool(data.get("group_clustered", 1))
        self.cluster_size = data.get("group_cluster_size", 1000)


class GroupParticipationAdapter(object):
    def __init__(self, data):
        self.participation_id = str(data.get("participation_id"))
        self.group_id = str(data.get("group_id"))
        self.group_class = str(data.get("group_class"))
        self.group_key = str(data.get("group_key"))
        self.cluster_id = int(data.get("cluster_id"))
        self.account = data.get("participation_account")
        self.role = data.get("participation_role")

    def calculate_recipient(self):
        if self.cluster_id:
            return str(self.group_key) + "-" + str(self.cluster_id)
        return str(self.group_key)


class GroupAndParticipationAdapter(GroupAdapter, GroupParticipationAdapter):
    def __init__(self, data):
        GroupAdapter.__init__(self, data)
        GroupParticipationAdapter.__init__(self, data)


class GroupsModel(Model):

    MESSAGE_PLAYER_JOINED = "player_joined"
    MESSAGE_PLAYER_LEFT = "player_left"

    def __init__(self, db, app):
        self.db = db
        self.cluster = Cluster(db, "group_clusters", "group_cluster_accounts")
        self.cluster_size = options.group_cluster_size
        self.app = app
        self.history = app.history
        self.online = None

    def get_setup_tables(self):
        return ["groups", "group_participants", "group_clusters", "group_cluster_accounts"]

    def get_setup_db(self):
        return self.db

    def has_delete_account_event(self):
        return True

    @coroutine
    def accounts_deleted(self, gamespace, accounts, gamespace_only):
        try:
            if gamespace_only:
                yield self.db.execute(
                    """
                        DELETE FROM `group_participants`
                        WHERE `gamespace_id`=%s AND `participation_account` IN %s;
                    """, gamespace, accounts)
            else:
                yield self.db.execute(
                    """
                        DELETE FROM `group_participants`
                        WHERE `participation_account` IN %s;
                    """, accounts)
        except DatabaseError as e:
            raise MessageError(500, "Failed to delete messages: " + e.args[1])

    @coroutine
    @validate(gamespace="int", group_class="str", key="str", clustered="bool", cluster_size="int")
    def new_group(self, gamespace, group_class, key, clustered=False, cluster_size=1000):

        try:
            group_id = yield self.db.insert(
                """
                    INSERT INTO `groups`
                    (`gamespace_id`, `group_class`, `group_key`,
                        `group_clustered`, `group_cluster_size`)
                    VALUES (%s, %s, %s, %s, %s);
                """, gamespace, group_class, key, int(bool(clustered)), cluster_size)
        except DuplicateError:
            raise GroupExistsError()
        except DatabaseError as e:
            raise GroupError(500, "Failed to add a group: " + e.args[1])
        else:
            raise Return(group_id)

    @coroutine
    def get_group(self, gamespace, group_id):
        try:
            message = yield self.db.get(
                """
                    SELECT *
                    FROM `groups`
                    WHERE `group_id`=%s AND `gamespace_id`=%s;
                """, group_id, gamespace)
        except DatabaseError as e:
            raise GroupError(500, "Failed to get a group: " + e.args[1])

        if not message:
            raise GroupNotFound()

        raise Return(GroupAdapter(message))

    @coroutine
    @validate(gamespace="int", group_class="str", key="str")
    def find_group(self, gamespace, group_class, key):
        try:
            group = yield self.db.get(
                """
                    SELECT *
                    FROM `groups`
                    WHERE `gamespace_id`=%s AND `group_class`=%s AND `group_key`=%s;
                """, gamespace, group_class, key)
        except DatabaseError as e:
            raise GroupError(500, "Failed to find a group: " + e.args[1])

        if not group:
            raise GroupNotFound()

        raise Return(GroupAdapter(group))

    @coroutine
    @validate(gamespace="int", group_class="str", key="str", account_id="int")
    def find_group_with_participation(self, gamespace, group_class, key, account_id):
        try:
            group = yield self.db.get(
                """
                    SELECT *
                    FROM `groups`
                        LEFT JOIN `group_participants`
                            ON `groups`.`group_id` = `group_participants`.`group_id`
                            AND `groups`.`gamespace_id` = `group_participants`.`gamespace_id`
                            AND `group_participants`.`participation_account`=%s

                    WHERE `groups`.`gamespace_id`=%s AND `groups`.`group_class`=%s
                        AND `groups`.`group_key`=%s;
                """, account_id, gamespace, group_class, key)
        except DatabaseError as e:
            raise GroupError(500, "Failed to find a group: " + e.args[1])

        if not group:
            raise GroupNotFound()

        if not group["participation_id"]:
            raise GroupParticipantNotFound()

        raise Return(GroupAndParticipationAdapter(group))

    @coroutine
    @validate(gamespace="int", group_class="str")
    def list_groups(self, gamespace, group_class):
        try:
            groups = yield self.db.query(
                """
                    SELECT *
                    FROM `groups`
                    WHERE `group_class`=%s AND `gamespace_id`=%s;
                """, group_class, gamespace)
        except DatabaseError as e:
            raise GroupError(500, "Failed to list groups: " + e.args[1])

        raise Return(map(GroupAdapter, groups))

    @coroutine
    @validate(gamespace_id="int", group=GroupAdapter)
    def delete_group(self, gamespace_id, group):

        group_id = group.group_id

        try:
            yield self.history.delete_messages_like(
                gamespace_id, group.group_class, group.key + "-%")
        except MessageError as e:
            raise GroupError(500, "Failed to delete group's messages: " + e.message)

        try:
            yield self.db.execute(
                """
                    DELETE FROM `groups`
                    WHERE `group_id`=%s AND `gamespace_id`=%s;
                """, group_id, gamespace_id)
        except DatabaseError as e:
            raise GroupError(500, "Failed to delete a group: " + e.args[1])

    @coroutine
    @validate(gamespace="int", group_id="int", group_class="str", key="str", cluster_size="int")
    def update_group(self, gamespace, group_id, group_class, key, cluster_size):
        try:
            yield self.db.execute(
                """
                    UPDATE `groups`
                    SET `group_class`=%s, `group_key`=%s, `group_cluster_size`=%s
                    WHERE `gamespace_id`=%s AND `group_id`=%s;
                """, group_class, key, cluster_size, gamespace, group_id)
        except DatabaseError as e:
            raise GroupError(500, "Failed to update a group: " + e.args[1])

    @coroutine
    @validate(gamespace="int", group=GroupAdapter, account="int", role="str", notify="json_dict", authoritative="bool")
    def join_group(self, gamespace, group, account, role, notify=None, authoritative=False):

        group_id = group.group_id

        if group.clustered:
            cluster_id = yield self.cluster.get_cluster(
                gamespace, account, group_id,
                cluster_size=group.cluster_size, auto_create=True)
        else:
            cluster_id = 0

        try:
            participation_id = yield self.db.execute(
                """
                    INSERT INTO `group_participants`
                    (gamespace_id, `group_id`, `group_class`, `group_key`,
                        `participation_account`, `participation_role`, `cluster_id`)
                    VALUES (%s, %s, %s, %s, %s, %s, %s);
                """, gamespace, group_id, group.group_class, group.key, account, role, cluster_id)
        except DuplicateError:
            raise UserAlreadyJoined()
        except DatabaseError as e:
            raise GroupError(500, "Failed to join a group: " + e.args[1])

        participation = GroupParticipationAdapter({
            "participation_id": participation_id,
            "group_id": group_id,
            "group_class": group.group_class,
            "group_key": group.key,
            "cluster_id": cluster_id,
            "account": account,
            "role": role
        })

        yield self.online.bind_account_to_group(account, participation)

        if notify:
            yield self.app.message_queue.add_message(
                gamespace, account, group.group_class, participation.calculate_recipient(),
                GroupsModel.MESSAGE_PLAYER_JOINED, notify, MessageFlags(),
                authoritative=authoritative)

        raise Return(participation)

    @coroutine
    @validate(gamespace="int", participation_id="int")
    def get_group_participation(self, gamespace, participation_id):
        try:
            participant = yield self.db.get(
                """
                    SELECT *
                    FROM `group_participants`
                    WHERE `participation_id`=%s AND `gamespace_id`=%s;
                """, participation_id, gamespace)
        except DatabaseError as e:
            raise GroupError(500, "Failed to get group participant: " + e.args[1])

        if not participant:
            raise GroupParticipantNotFound()

        raise Return(GroupParticipationAdapter(participant))

    @coroutine
    @validate(gamespace="int", participation_id="int", role="str")
    def updated_group_participation(self, gamespace, participation_id, role):
        try:
            yield self.db.execute(
                """
                    UPDATE `group_participants`
                    SET `participation_role`=%s
                    WHERE `gamespace_id`=%s AND `participation_id`=%s;
                """, role, gamespace, participation_id)
        except DatabaseError as e:
            raise GroupError(500, "Failed to update a group participation: " + e.args[1])

    @coroutine
    @validate(gamespace="int", group=GroupAdapter, account="int", notify="json_dict", authoritative="bool")
    def leave_group(self, gamespace, group, account, notify=None, authoritative=False):

        participation = yield self.find_group_participant(gamespace, group.group_id, account)

        try:
            yield self.db.execute(
                """
                    DELETE FROM `group_participants`
                    WHERE `gamespace_id`=%s AND `participation_id`=%s;
                """, gamespace, participation.participation_id)
        except DatabaseError as e:
            raise GroupError(500, "Failed to leave a group: " + e.args[1])

        if participation.cluster_id:
            try:
                yield self.cluster.leave_cluster(
                    gamespace, account, group.group_id)
            except ClusterError:
                # well
                pass

        if notify:
            yield self.app.message_queue.add_message(
                gamespace, account, group.group_class, participation.calculate_recipient(),
                GroupsModel.MESSAGE_PLAYER_LEFT, notify, MessageFlags(),
                authoritative=authoritative)

    @coroutine
    @validate(gamespace="int", group_id="int", account="int")
    def find_group_participant(self, gamespace, group_id, account):
        try:
            participant = yield self.db.get(
                """
                    SELECT *
                    FROM `group_participants`
                    WHERE `group_id`=%s AND `participation_account`=%s AND `gamespace_id`=%s;
                """, group_id, account, gamespace)
        except DatabaseError as e:
            raise GroupError(500, "Failed to get group participant: " + e.args[1])

        if not participant:
            raise GroupParticipantNotFound()

        raise Return(GroupParticipationAdapter(participant))

    @coroutine
    @validate(gamespace="int", group_id="int")
    def list_group_participants(self, gamespace, group_id):
        try:
            participants = yield self.db.query(
                """
                    SELECT *
                    FROM `group_participants`
                    WHERE `group_id`=%s AND `gamespace_id`=%s;
                """, group_id, gamespace)
        except DatabaseError as e:
            raise GroupError(500, "Failed to list group participants: " + e.args[1])

        raise Return(map(GroupParticipationAdapter, participants))

    @coroutine
    @validate(gamespace="int", account_id="int")
    def list_groups_account_participates(self, gamespace, account_id):
        try:
            groups = yield self.db.query(
                """
                    SELECT g.*, p.*
                    FROM `group_participants` AS p
                        INNER JOIN `groups` AS g
                        ON p.`group_id`=`g`.`group_id`
                    WHERE p.`participation_account`=%s AND p.`gamespace_id`=%s;
                """, account_id, gamespace)
        except DatabaseError as e:
            raise GroupError(500, "Failed to list group account participate: " + e.args[1])

        raise Return(map(GroupAndParticipationAdapter, groups))

    @coroutine
    @validate(gamespace="int", account_id="int")
    def list_participants_by_account(self, gamespace, account_id):
        try:
            participants = yield self.db.query(
                """
                    SELECT *
                    FROM `group_participants`
                    WHERE `participation_account`=%s AND `gamespace_id`=%s;
                """, account_id, gamespace)
        except DatabaseError as e:
            raise GroupError(500, "Failed to list group account participate: " + e.args[1])

        raise Return(map(GroupParticipationAdapter, participants))


class GroupNotFound(Exception):
    pass


class GroupParticipantNotFound(Exception):
    pass


class GroupExistsError(Exception):
    pass


class UserAlreadyJoined(Exception):
    pass


class GroupError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message

    def __str__(self):
        return str(self.code) + ": " + self.message
