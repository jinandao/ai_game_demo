"""行为域：场景、元素与场景图。

一屏一场景，角色按 (scene, x, y) 定位，移动走场景图最短路。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 行为脚本里表示"玩家当前所在处"的保留目标
PLAYER = "$player"

# 一屏的尺寸
SCREEN_W, SCREEN_H = 1440, 810
INDOOR_ENTRY = (720, 700)


@dataclass(frozen=True)
class Prop:
    """场景里的一个元素。建筑、树、湖、长椅都是 Prop。"""

    id: str
    name: str
    x: int
    y: int
    w: int
    h: int
    kind: str = "prop"          # building | tree | water | prop
    interact: str = ""           # look | sit | enter
    target: str = ""             # interact=enter 时的目标场景
    text: str = ""               # interact=look 时的一句话

    @property
    def rect(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.w, self.h

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2


@dataclass(frozen=True)
class Scene:
    id: str
    name: str
    kind: str                    # outdoor | indoor | private
    background: str
    # 这个场景此刻"客观是这样"的一段短描述：天气、光线、气味、声音。
    # 有它，模型聊起环境时就有依据，不会自己发明天气又前后矛盾。
    ambience: str = ""
    props: tuple[Prop, ...] = ()
    doors: dict[str, tuple[int, int]] = field(default_factory=dict)
    entry: tuple[int, int] = INDOOR_ENTRY
    tags: tuple[str, ...] = ()


# 兼容旧代码里的类型名
Place = Scene


class World:
    def __init__(self, scenes: list[Scene], links: dict[str, tuple[str, ...]] | None = None) -> None:
        self._scenes = {s.id: s for s in scenes}
        self._links = links or {}

    # ── 查询 ──

    def scene(self, sid: str) -> Scene:
        if sid not in self._scenes:
            raise KeyError(f"未知场景: {sid}")
        return self._scenes[sid]

    # 旧接口名
    def place(self, sid: str) -> Scene:
        return self.scene(sid)

    def has(self, sid: str) -> bool:
        return sid in self._scenes

    def all(self) -> list[Scene]:
        return list(self._scenes.values())

    def by_tag(self, tag: str) -> list[Scene]:
        return [s for s in self._scenes.values() if tag in s.tags]

    # ── 解析 ──

    def resolve(self, target: str, *, agent_place: str, player_place: str) -> str:
        """把脚本里的目标解析成真实场景 id。

        支持：场景 id、"$player"、以及 "?"（就近的安静处）。
        """
        if target == PLAYER:
            return player_place
        if target in ("?", "any", "nearby"):
            quiet = [s for s in self.by_tag("quiet") if s.id != agent_place]
            return quiet[0].id if quiet else agent_place
        if not self.has(target):
            return agent_place      # 容错：不认识的去处原地不动，不要炸
        return target

    # ── 场景图 ──

    def links(self, sid: str) -> tuple[str, ...]:
        return self._links.get(sid, ())

    def path(self, start: str, goal: str) -> list[str]:
        """BFS 最短路。场景只有五个，BFS 足够。"""
        if start == goal:
            return [start]
        queue: list[list[str]] = [[start]]
        seen = {start}
        while queue:
            route = queue.pop(0)
            for nxt in self.links(route[-1]):
                if nxt in seen:
                    continue
                if nxt == goal:
                    return route + [nxt]
                seen.add(nxt)
                queue.append(route + [nxt])
        return [start]

    def door(self, src: str, dst: str) -> tuple[int, int]:
        """src 场景里通往 dst 的门点坐标。找不到就返回场景中心。"""
        scene = self.scene(src)
        if dst in scene.doors:
            return scene.doors[dst]
        return SCREEN_W // 2, SCREEN_H - 60

    def entry(self, sid: str) -> tuple[int, int]:
        return self.scene(sid).entry

    # ── 旧接口：距离（秒）。新移动模型不再用它，保留以免上游报错 ──

    def distance(self, a: str, b: str) -> float:
        return float(len(self.path(a, b)) - 1) * 8.0


def _trees() -> tuple[Prop, ...]:
    """树林带：一排独立树，只做视觉，不挡路。"""
    out = []
    for i in range(9):
        out.append(
            Prop(
                id=f"tree_{i}",
                name="树",
                x=40 + i * 165,
                y=60 + (i % 3) * 24,
                w=120,
                h=200,
                kind="tree",
            )
        )
    return tuple(out)


def base_world() -> World:
    outdoor = Scene(
        id="outdoor",
        name="站前空地",
        kind="outdoor",
        background="assets/bg_outdoor.png",
        ambience="天是晴的，风不大。太阳有点晃眼，空气里有铁锈和草晒过的味道。",
        props=(
            *_trees(),
            Prop("board", "全息广告牌", 600, 250, 190, 90, "prop", "look",
                 text="十九年前的饮料广告，循环到今天。广告里的女孩一直在笑。"),
            Prop("container", "集装箱", 150, 280, 240, 150, "building", "enter",
                 target="lan_room", text="门上有一道新的划痕。"),
            Prop("workshop_b", "修理间", 900, 270, 250, 210, "building", "enter",
                 target="workshop", text="门缝里漏出一点灯光。"),
            Prop("canteen_b", "食堂", 1180, 470, 230, 190, "building", "enter",
                 target="canteen", text="招牌上的字掉了两个。"),
            Prop("stall", "交换所", 560, 620, 160, 90, "prop", "look",
                 text="一张旧折叠桌，几箱东西摆着。陈嫂今天不在。"),
            Prop("gate", "闸门", 60, 720, 200, 70, "prop", "look",
                 text="两扇铁门。开一次要两个人推。门外的轨道一直伸进城里。"),
            Prop("lake", "蓄水池", 60, 500, 380, 260, "water", "sit",
                 text="水很静。风一过，整片天就碎一下。"),
            Prop("bench", "长椅", 620, 500, 120, 50, "prop", "sit",
                 text="坐上去会晃。垫了一块砖。"),
            Prop("laundry", "晾衣绳", 760, 330, 180, 60, "prop", "look",
                 text="几件洗得发白的衣服，被风吹得贴着绳子。"),
        ),
        doors={"workshop": (1025, 480), "canteen": (1295, 660), "lan_room": (270, 430)},
        entry=(720, 660),
        tags=("open", "busy"),
    )

    workshop = Scene(
        id="workshop",
        name="修理间",
        kind="indoor",
        background="assets/bg_workshop.png",
        ambience="屋里有灯，空气里有机油味和金属屑。外面的事听不太清楚。",
        props=(
            Prop("workbench", "工作台", 260, 380, 320, 140, "prop", "look",
                 text="摊着一只拆到一半的义体手，螺丝按大小排开。"),
            Prop("shelf", "零件柜", 980, 320, 260, 220, "prop", "look",
                 text="三排架子，按大小排，不按用途。"),
            Prop("cat", "螺丝", 700, 560, 90, 70, "prop", "look",
                 text="机械猫只剩一条前腿会动。它歪头看你。"),
            Prop("lamp", "那盏灯", 640, 200, 60, 80, "prop", "look",
                 text="灯一直亮着。你从没见它关过。"),
            Prop("back_door", "里间", 1200, 560, 140, 180, "building", "enter",
                 target="mio_room", text="门后有很轻的哼唱声。"),
            Prop("door_out", "出口", 620, 730, 200, 80, "door", "enter",
                 target="outdoor", text="回到外面。"),
        ),
        doors={"outdoor": (720, 760), "mio_room": (1270, 740)},
        entry=(720, 700),
        tags=("quiet", "tools", "light"),
    )

    canteen = Scene(
        id="canteen",
        name="食堂",
        kind="indoor",
        background="assets/bg_canteen.png",
        ambience="灶上还温着，屋里有饭菜味。窗玻璃上蒙着一层水汽。",
        props=(
            Prop("table1", "长桌", 260, 420, 300, 90, "prop", "sit", text="桌腿不齐，垫着纸。"),
            Prop("table2", "长桌", 760, 420, 300, 90, "prop", "sit", text="桌腿不齐，垫着纸。"),
            Prop("stove", "灶台", 1140, 300, 220, 160, "prop", "look", text="老赵的地盘。锅里有热气。"),
            Prop("notice", "墙上的纸", 480, 220, 200, 90, "prop", "look", text="「不许剩饭」。字是陈嫂写的。"),
            Prop("door_out", "出口", 620, 730, 200, 80, "door", "enter",
                 target="outdoor", text="回到外面。"),
        ),
        doors={"outdoor": (720, 760)},
        entry=(720, 700),
        tags=("warm", "busy", "light"),
    )

    lan_room = Scene(
        id="lan_room",
        name="岚的房间",
        kind="private",
        background="assets/bg_lan_room.png",
        ambience="集装箱里很静。只有门缝透进来的一道光是亮的，其余都在阴影里。",
        props=(
            Prop("bed", "床", 260, 400, 280, 140, "prop", "sit", text="铺得很整齐，和人不搭。"),
            Prop("map", "墙上的地图", 600, 200, 300, 180, "prop", "look",
                 text="铅笔画满了线，北边有几处被擦破了。"),
            Prop("locker", "铁柜", 1040, 360, 200, 200, "prop", "look",
                 text="锁着。第二层抽屉从来不打开。"),
            Prop("door_out", "出口", 620, 730, 200, 80, "door", "enter",
                 target="outdoor", text="回到外面。"),
        ),
        doors={"outdoor": (720, 760)},
        entry=(720, 700),
        tags=("quiet", "private"),
    )

    mio_room = Scene(
        id="mio_room",
        name="澪的隔间",
        kind="private",
        background="assets/bg_mio_room.png",
        ambience="屋子很小，一盏壁灯一直亮着，空气有点闷。",
        props=(
            Prop("bed", "床", 300, 420, 260, 130, "prop", "sit", text="很小，被子叠得像方块。"),
            Prop("lamp", "灯", 620, 240, 60, 80, "prop", "look", text="整夜亮着。"),
            Prop("books", "书堆", 900, 420, 220, 150, "prop", "look",
                 text="全是旧的接口手册，页角翻烂了。"),
            Prop("drawer", "抽屉", 1180, 420, 160, 140, "prop", "look",
                 text="上锁。你不知道里面是什么。"),
            Prop("door_out", "出口", 620, 730, 200, 80, "door", "enter",
                 target="workshop", text="回到修理间。"),
        ),
        doors={"workshop": (720, 760)},
        entry=(720, 700),
        tags=("quiet", "private", "light"),
    )

    return World(
        [outdoor, workshop, canteen, lan_room, mio_room],
        links={
            "outdoor": ("workshop", "canteen", "lan_room"),
            "workshop": ("outdoor", "mio_room"),
            "canteen": ("outdoor",),
            "lan_room": ("outdoor",),
            "mio_room": ("workshop",),
        },
    )
