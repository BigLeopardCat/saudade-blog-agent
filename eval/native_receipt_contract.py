"""Small native receipt facts shared by offline golden assertions.

These mappings describe structured target IDs emitted by migrated domains; they
are not an intent vocabulary or a planner intermediate representation.
"""

TARGET_FIELDS = {
    "audit_board_comment": "talk_id",
    "approve_quota_request": "user_id",
    "reject_quota_request": "user_id",
    "audit_comment": "comment_id",
}
FIELD_FAMILY = {"talk_id": "board", "user_id": "quota", "comment_id": "comment"}
TAG_FAMILY = {"talkId": "board", "userId": "quota", "commentId": "comment"}
