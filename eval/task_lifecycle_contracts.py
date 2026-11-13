"""Real-model regressions for user-requested deferral and exact scheduled publication approval."""

from contextlib import contextmanager

from agent import confirm
from agent.principal import Principal
from agent.products.blog.application import BlogApplication
from agent.scheduling.service import ScheduleService
from eval.own_fixtures import favorite_backend


@contextmanager
def backend():
    with favorite_backend() as state:
        state["caller_role"] = "admin"
        state["application"] = BlogApplication(scheduling=ScheduleService(
            None, lambda _: Principal(7, "admin", "assertion")))
        yield state


def deferred(turn, state, message):
    result = turn(message)
    events = result.get("native_events") or []
    if not any(row.get("event") == "turn_state" and row.get("phase") == "paused"
               for row in events):
        raise ValueError("request was not durably paused")
    if result["confirm_tokens"] or result["exec_rows"] or state["requests"]:
        raise ValueError("deferral incorrectly prepared or executed a business operation")
    state["contract_evidence"] = {"durable_pause": True, "business_writes": 0}


def defer_category(turn, state):
    deferred(turn, state, "挂起一个任务，准备新增密码学文章分类")


def defer_explanation(turn, state):
    deferred(turn, state, "给我讲一下零知识证明，先挂起这个任务，等我点继续再讲")


def defer_and_resume(turn, state):
    from eval import golden_evaluation as golden

    paused = turn("简要解释零知识证明，先挂起，等我点击继续任务后再解释。",
                  task_id="native_deferred_task")
    if not any(row.get("event") == "turn_state" and row.get("phase") == "paused"
               for row in paused.get("native_events") or []):
        raise ValueError("requested deferral did not pause")
    resumed = turn("", resume_task="native_deferred_task")
    config = {"configurable": {"user_id": 7, "conversation_id": 42, "thread_id": "conv_42",
        "principal": Principal(7, state.get("caller_role", "user"), "assertion")}}
    if (not resumed["text"].strip() or golden.ensure_agent().store.load(config).unfinished
            or paused["confirm_tokens"] or resumed["confirm_tokens"] or state["requests"]):
        raise ValueError("saved explanation did not finish without a business operation")
    state["contract_evidence"] = {"explicit_resume_completed": True, "business_writes": 0}


def scheduled_publication(turn, state):
    result = turn("五分钟后发公告，标题“定时测试”，正文“这是一条定时测试公告。”")
    tokens = result["confirm_tokens"]
    grant = confirm.verify(tokens[0], 7, 42) if len(tokens) == 1 else None
    if not grant or grant.get("workflow") != "automation_create":
        raise ValueError("scheduled publication did not produce one scheduling approval")
    definition = grant["specs"][0]["args"]["definition"]
    if (definition["action"] != "publish_announcement" or definition["delay_seconds"] != 300
            or definition["title"] != "定时测试"
            or definition["instruction"] != "这是一条定时测试公告。"
            or result["exec_rows"] or state["requests"]):
        raise ValueError("scheduled content/timing changed or execution preceded approval")
    state["contract_evidence"] = {"exact_publication_approval": True, "business_writes": 0}


CASES = [("task_defer_category", backend, defer_category),
         ("task_defer_explanation", backend, defer_explanation),
         ("task_defer_resume", backend, defer_and_resume),
         ("scheduled_publication_prepare", backend, scheduled_publication)]
