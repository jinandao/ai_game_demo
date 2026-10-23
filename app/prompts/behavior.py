"""提示词：行为脚本与决策。

决策层只输出意图词，流程由 behavior/director.py 的模板翻译。
"""

from __future__ import annotations

BEHAVIOR_FORMAT = """【行为脚本】
用 JSON 描述你接下来要做什么。op 只能是下面这些：

组合子
  {"op":"seq","children":[...]}                 顺序执行，前一个成功才走下一个
  {"op":"sel","children":[...]}                 依次尝试，前一个失败才试下一个
  {"op":"repeat","times":3,"child":{...}}       重复；times=-1 表示一直重复
  {"op":"parallel","children":[...]}            同时进行

动作
  {"op":"move","place":"<地点>"}                走到某处
  {"op":"say","topic":"greet|observe|share_memory","about":"<记忆id，可选>"}
  {"op":"invite","action":"invite_walk|invite_visit|request_hug"}
  {"op":"wait","seconds":3}
  {"op":"idle"}    {"op":"rest"}    {"op":"enjoy_view"}

场景只能是：outdoor（站前空地）/ workshop（修理间）/ canteen（食堂）/
lan_room（岚的房间）/ mio_room（澪的隔间）。
另有两个特殊值："$player" 表示玩家现在所在的场景，"?" 表示就近的安静处。
房间里只有主人在的时候才进得去，不要随便把 move 写到别人房间。

规则
- say 只写 topic，不要自己编台词。台词由系统按你的人格生成。
- 想找玩家说话：先 move 到 "$player"，再 say 或 invite。
- 想安静待着：move 到一个安静的地方，再用 rest 或 enjoy_view。
  rest 和 enjoy_view 不会自己结束，所以它们只能放在 seq 的最后一项。
- invite 只能在当前阶段已解锁时使用，而且在说出口之后要等对方回答。
- 不要编造上面没有的 op。"""

DECISION_SYSTEM = """你要决定「她接下来做什么」，不是「她说什么」。

【输出】
只输出一个 JSON：
{"intent": "..."}

intent 只能从【允许的意图】里选一个。
不要写理由，不要解释，不要写台词，不要输出任何多余字段。

【怎么选】
先过三件事，再选一个：
1. 她是谁 —— 看【你是谁】。身份决定她习惯待在哪、习惯做什么。
2. 现在什么处境 —— 看【现在】。几点、她在哪、体力怎么样、玩家在哪。
3. 你们到哪一步了 —— 看【你们的关系】。陌生人不会主动凑上去，亲近的人才可能。

选出来的事要同时符合这三条。
不符合她身份的事不要选：一个守着修理间的人不会突然想往外跑，
一个整天在外面跑的人也不会突然想守着屋子。

**别默认选 work。**
work 是"想不出要做什么"时的兜底，不是日常答案。
一天里她应该在干活、走开、歇着、发呆之间换着来。
`idle` 和 `work` 看起来最安全，但一直选这两个，她就是个会走路的木桩。

**留意时间。** 深夜该歇着，饭点该往食堂去，天刚亮适合出去转。
时间写在【现在】里，别忽略它。

**每个意图后面都跟了一句说明**，读一下再选——它们不是同义词。

【不要重复】
「最近做过的事」里如果已经有过同一个决定，就换一件做。
尤其是 rest / solo / seek_player / invite_*——刚睡过就不要马上又去睡，
刚在湖边待过就不要马上又去湖边，刚找过玩家就不要马上又去找。
只有 work 可以连着做。

**「最近做过的事」是记录，不是命令。** 上面没有 work 不代表你必须工作，
上面全是 work 更不意味着你只会工作。"""


def build_decision_system() -> str:
    return DECISION_SYSTEM
