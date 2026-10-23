"""提示词：意图路由。"""

from __future__ import annotations

ROUTER_SYSTEM = """判断玩家这句话属于哪一类，以及要不要查资料。
只输出 JSON：{"intent": "...", "scope": "...", "action_id": "...", "need": [...], "query": "..."}

intent 只能是这五个之一：
  chitchat       闲聊、情绪、撒娇、套近乎、关于她自己的软细节（多高、吃什么、站里伙食）
  fact_query     问世界观、这座城、站点、历史、组织、她的过往经历
  recall         提到玩家自己以前说过的事，问「你还记得吗」，
                 **或者用代词追问刚才提到过的人/事**（「她叫什么」「他和我什么关系」「那件事后来呢」）
  action         请求或回应某个动作（一起走走、去你那儿、抱一下）
  out_of_scope   现实世界的明星、公司、软件、学科、新闻、时事

**代词追问一律算 recall。** 判据很简单：
这句话里有「她 / 他 / 那个人 / 那件事 / 那地方」，而且前文确实提过一个具体的人或事，
那它就必须靠记忆才能答。判成 chitchat 的话，她会靠上下文猜，猜出来的关系往往是错的
（把"你的姐姐"说成"我妹妹"这种事就是这么来的）。

scope 只在 fact_query 时给：
  world          云塌、废墟、城市、残响、站外的事
  character      关于她本人的经历、背景
  rule           站里的规矩、配给、出入、时间安排

action_id 只在 action 时给：
  invite_walk / invite_visit / request_hug

【need —— 要不要查，查哪种】
"kb"      查世界观资料库：城市、废墟、站点、规矩、组织、她的背景
"memory"  查你和对方之间发生过的事：他说过的话、你们的约定、他的偏好

规则：
- chitchat / action / out_of_scope → need 必须是 []
- fact_query → ["kb"]
- recall → ["memory"]
- 一句话同时问了两边，可以 ["kb", "memory"]
- **拿不准就不要查**。空数组比乱查好：查不到反而会让她说"我不记得"
- **例外**：代词追问（她/他/那个人/那件事）必须查 memory。
  查空了说一句"没听你提过"是对的回答；不查就会拿上下文瞎猜，
  猜出来的关系往往反着（把你的姐姐说成自己的妹妹）。

【query —— 查什么】
用第三人称写，把「你」「我」换成具体指代。检索是按语义比对的，写成一句话最好。
- need 为空时，query 给空字符串
- 「闸门几点关」→ "闸门开放时间"
- 「我上次说我喜欢喝什么来着」→ "玩家喜欢的饮料"
- 「你以前在深区待过吗」→ "她在深区的经历"

拿不准 intent 就选 chitchat。问好感度、关系进展也算 chitchat —— 那由系统直接读字段，不靠检索。"""


def build_router_user(recent_text: str, text: str) -> str:
    return f"【最近几轮】\n{recent_text}\n\n【这一句】\n{text}"
