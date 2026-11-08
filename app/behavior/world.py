"""场景、元素与场景图。"""

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
    ambience: str = ""
    props: tuple[Prop, ...] = ()
    doors: dict[str, tuple[int, int]] = field(default_factory=dict)
    entry: tuple[int, int] = INDOOR_ENTRY
    tags: tuple[str, ...] = ()
    # 世界尺寸。大于屏幕时渲染器开相机跟着玩家走。
    w: int = SCREEN_W
    h: int = SCREEN_H

    @property
    def size(self) -> tuple[int, int]:
        return self.w, self.h

    @property
    def scrollable(self) -> bool:
        """要不要开相机。"""
        return self.w > SCREEN_W or self.h > SCREEN_H

    @property
    def walk_top(self) -> int:
        """可行走上边界：室外从树林带下面开始，室内从墙脚线开始。"""
        return 240 if self.kind == "outdoor" else 260

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
        """把脚本里的目标解析成真实场景 id。"""
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
        """src 场景里通往 dst 的门点坐标。找不到就返回场景底部中央。"""
        scene = self.scene(src)
        if dst in scene.doors:
            return scene.doors[dst]
        return scene.w // 2, scene.h - 60

    def entry(self, sid: str) -> tuple[int, int]:
        return self.scene(sid).entry

    def bounds(self, sid: str) -> tuple[int, int, int, int]:
        """可行走矩形 (left, top, right, bottom)。"""
        s = self.scene(sid)
        return 20, s.walk_top, s.w - 20, s.h - 24

    # ── 旧接口：距离（秒）。新移动模型不再用它，保留以免上游报错 ──

    def distance(self, a: str, b: str) -> float:
        return float(len(self.path(a, b)) - 1) * 8.0

def _trees(width: int = 2880) -> tuple[Prop, ...]:
    """树林带：一排独立树，只做视觉，不挡路。"""
    out = []
    n = 13
    for i in range(n):
        out.append(
            Prop(
                id=f"tree_{i}",
                name="树",
                x=20 + i * 220,
                y=40 + (i % 3) * 30,
                w=150,
                h=240,
                kind="tree",
            )
        )
    return tuple(out)

def base_world() -> World:
    # 户外 2880×1620：横着刚好两屏、竖着两屏，走一圈要一分多钟。
    outdoor = Scene(
        id="outdoor",
        name="站前空地",
        kind="outdoor",
        background="assets/bg_outdoor.png",
        ambience="天是晴的，风不大。太阳有点晃眼，空气里有铁锈和草晒过的味道。",
        props=(
            *_trees(2880),
            Prop("board", "全息广告牌", 1120, 320, 300, 130, "prop", "look",
                 text="十九年前的饮料广告，循环到今天。广告里的女孩一直在笑。"),
            Prop("pylon", "电线杆", 1640, 460, 90, 300, "prop", "look",
                 text="杆子上钉着几块褪色的指示牌，箭头都指错了方向。"),
            Prop("container", "集装箱", 180, 560, 340, 220, "building", "enter",
                 target="lan_room", text="门上有一道新的划痕。"),
            Prop("workshop_b", "修理间", 760, 420, 380, 300, "building", "enter",
                 target="workshop", text="门缝里漏出一点灯光。"),
            Prop("laundry", "晾衣绳", 700, 820, 280, 100, "prop", "look",
                 text="几件洗得发白的衣服，被风吹得贴着绳子。"),
            Prop("planter", "菜箱", 460, 640, 220, 110, "prop", "look",
                 text="废轮胎摞起来填了土，种着几棵长得不太好的青菜。"),
            Prop("stall", "交换所", 1120, 900, 260, 140, "prop", "look",
                 text="一张旧折叠桌，几箱东西摆着。陈嫂今天不在。"),
            Prop("bench", "长椅", 1480, 960, 220, 80, "prop", "sit",
                 text="坐上去会晃。垫了一块砖。"),
            Prop("canteen_b", "食堂", 2180, 700, 380, 300, "building", "enter",
                 target="canteen", text="招牌上的字掉了两个。"),
            Prop("garden_gate", "铁皮围的一小块", 300, 1080, 280, 110, "prop", "enter",
                 target="garden", text="土是从废墟一车一车推回来的。里面有花开着。"),
            Prop("cart", "板车", 900, 1180, 240, 130, "prop", "look",
                 text="轮子歪了一个，一直没人修。上面堆着捆好的纸板。"),
            Prop("hall_b", "城镇大厅", 1280, 1140, 340, 240, "building", "enter",
                 target="hall", text="原来是个超市，货架拆了改成一排摊位。"),
            Prop("storage_b", "杂物间", 1760, 1240, 360, 220, "building", "enter",
                 target="storage", text="门上用记号笔写着歪歪扭扭的「杂物」两个字。"),
            Prop("lake", "蓄水池", 2200, 1240, 560, 340, "water", "sit",
                 text="水很静。风一过，整片天就碎一下。"),
            Prop("gate", "闸门", 80, 1440, 320, 100, "prop", "enter",
                 target="ruins", text="两扇铁门。开一次要两个人推。门外的轨道一直伸进城里。"),
        ),
        doors={
            "workshop": (950, 760),
            "canteen": (2370, 1040),
            "lan_room": (350, 820),
            "hall": (1450, 1420),
            "storage": (1940, 1500),
            "garden": (440, 1230),
            "ruins": (240, 1560),
        },
        entry=(1440, 900),
        tags=("open", "busy"),
        w=2880,
        h=1620,
    )

    workshop = Scene(
        id="workshop",
        name="修理间",
        kind="indoor",
        background="assets/bg_workshop.png",
        ambience="屋里有灯，空气里有机油味和金属屑。外面的事听不太清楚。",
        props=(
            Prop("workbench", "工作台", 300, 480, 420, 190, "prop", "craft",
                 text="摊着一只拆到一半的义体手，螺丝按大小排开。"),
            Prop("shelf", "零件柜", 1240, 390, 340, 280, "prop", "look",
                 text="三排架子，按大小排，不按用途。"),
            Prop("cat", "螺丝", 880, 700, 130, 100, "prop", "look",
                 text="机械猫只剩一条前腿会动。它歪头看你。"),
            Prop("lamp", "那盏灯", 820, 250, 80, 110, "prop", "look",
                 text="灯一直亮着。你从没见它关过。"),
            Prop("back_door", "里间", 1500, 720, 220, 260, "building", "enter",
                 target="mio_room", text="门后有很轻的哼唱声。"),
            Prop("door_out", "出口", 780, 960, 260, 100, "door", "enter",
                 target="outdoor", text="回到外面。"),
        ),
        doors={"outdoor": (900, 990), "mio_room": (1610, 950)},
        entry=(900, 930),
        tags=("quiet", "tools", "light"),
        w=1800,
        h=1100,
    )

    canteen = Scene(
        id="canteen",
        name="食堂",
        kind="indoor",
        background="assets/bg_canteen.png",
        ambience="灶上还温着，屋里有饭菜味。窗玻璃上蒙着一层水汽。",
        props=(
            Prop("table1", "长桌", 280, 560, 380, 110, "prop", "sit", text="桌腿不齐，垫着纸。"),
            Prop("table2", "长桌", 920, 560, 380, 110, "prop", "sit", text="桌腿不齐，垫着纸。"),
            Prop("stove", "灶台", 1440, 380, 280, 200, "prop", "look", text="老赵的地盘。锅里有热气。"),
            Prop("notice", "墙上的纸", 620, 280, 240, 110, "prop", "look", text="「不许剩饭」。字是陈嫂写的。"),
            Prop("kitchen_door", "后厨", 1460, 780, 200, 200, "building", "enter",
                 target="kitchen", text="抽油烟机坏了，墙上一层油。"),
            Prop("door_out", "出口", 780, 960, 260, 100, "door", "enter",
                 target="outdoor", text="回到外面。"),
        ),
        doors={"outdoor": (900, 990), "kitchen": (1560, 950)},
        entry=(900, 930),
        tags=("warm", "busy", "light"),
        w=1800,
        h=1100,
    )

    lan_room = Scene(
        id="lan_room",
        name="岚的房间",
        kind="private",
        background="assets/bg_lan_room.png",
        ambience="集装箱里很静。只有门缝透进来的一道光是亮的，其余都在阴影里。",
        props=(
            Prop("bed", "床", 300, 480, 360, 180, "prop", "sit", text="铺得很整齐，和人不搭。"),
            Prop("map", "墙上的地图", 640, 250, 400, 230, "prop", "look",
                 text="铅笔画满了线，北边有几处被擦破了。"),
            Prop("locker", "铁柜", 1120, 440, 260, 260, "prop", "look",
                 text="锁着。第二层抽屉从来不打开。"),
            Prop("door_out", "出口", 650, 830, 260, 100, "door", "enter",
                 target="outdoor", text="回到外面。"),
        ),
        doors={"outdoor": (780, 880)},
        entry=(780, 800),
        tags=("quiet", "private"),
        w=1500,
        h=950,
    )

    mio_room = Scene(
        id="mio_room",
        name="澪的隔间",
        kind="private",
        background="assets/bg_mio_room.png",
        ambience="屋子很小，一盏壁灯一直亮着，空气有点闷。",
        props=(
            Prop("bed", "床", 340, 500, 320, 170, "prop", "sit", text="很小，被子叠得像方块。"),
            Prop("lamp", "灯", 720, 280, 80, 110, "prop", "look", text="整夜亮着。"),
            Prop("books", "书堆", 980, 500, 280, 190, "prop", "look",
                 text="全是旧的接口手册，页角翻烂了。"),
            Prop("drawer", "抽屉", 1240, 280, 200, 190, "prop", "look",
                 text="上锁。你不知道里面是什么。"),
            Prop("door_out", "出口", 650, 830, 260, 100, "door", "enter",
                 target="workshop", text="回到修理间。"),
        ),
        doors={"workshop": (780, 880)},
        entry=(780, 800),
        tags=("quiet", "private", "light"),
        w=1500,
        h=950,
    )

    kitchen = Scene(
        id="kitchen",
        name="后厨",
        kind="indoor",
        background="assets/bg_kitchen.png",
        ambience="抽油烟机坏了，墙上一层油。灶台是改的，用一个旧输液架挂着锅。",
        props=(
            Prop("stove_k", "灶台", 280, 460, 380, 210, "prop", "craft",
                 text="改装过的灶，火能开。锅挂在一个输液架上。"),
            Prop("cutting_board", "案板", 760, 530, 280, 120, "prop", "look",
                 text="切到一半的东西还摆着。"),
            Prop("pantry", "储物架", 1180, 400, 320, 280, "prop", "look",
                 text="米、干菜、几罐没标签的东西。"),
            Prop("door_out", "回食堂", 690, 870, 260, 100, "door", "enter",
                 target="canteen", text="回到食堂。"),
        ),
        doors={"canteen": (820, 920)},
        entry=(820, 840),
        tags=("warm", "tools", "light"),
        w=1600,
        h=1000,
    )

    storage = Scene(
        id="storage",
        name="杂物间",
        kind="indoor",
        background="assets/bg_storage.png",
        ambience="堆满了从废墟捡回来的东西。地上没有下脚的地方，走路要挑着踩。",
        props=(
            Prop("pile", "废料堆", 200, 500, 440, 260, "prop", "gather",
                 text="断了的机械臂、烧变形的电路板、成筐的螺帽。能翻。"),
            Prop("crate", "木箱", 880, 540, 300, 200, "prop", "look",
                 text="分好类的零件，标签是手写的。"),
            Prop("table_s", "矮桌", 1340, 510, 300, 180, "prop", "craft",
                 text="上面是半成品，做了一半就放着了。"),
            Prop("door_out", "出口", 780, 960, 260, 100, "door", "enter",
                 target="outdoor", text="回到外面。"),
        ),
        doors={"outdoor": (900, 990)},
        entry=(900, 930),
        tags=("tools", "quiet"),
        w=1800,
        h=1100,
    )

    hall = Scene(
        id="hall",
        name="城镇大厅",
        kind="indoor",
        background="assets/bg_hall.png",
        ambience="站里唯一像市场的地方。地上用粉笔画了格子，谁摆哪儿是排班排出来的。",
        props=(
            Prop("stall_food", "食品摊", 180, 500, 360, 250, "building", "shop",
                 text="干面包、净水、盐。价钱写在纸板上。"),
            Prop("stall_gear", "器械摊", 660, 520, 360, 240, "building", "shop",
                 text="扳手、电池、绷带，还有几件看着就不便宜的东西。"),
            Prop("counter", "交换柜台", 1180, 530, 360, 230, "building", "shop",
                 text="站里管交换的地方。收东西给票据，拿票据换东西。"),
            Prop("notice_wall", "布告墙", 1660, 380, 260, 300, "prop", "look",
                 text="贴满了纸：找人、找东西、找活干。"),
            Prop("door_out", "出口", 870, 1060, 260, 100, "door", "enter",
                 target="outdoor", text="回到外面。"),
        ),
        doors={"outdoor": (1000, 1090)},
        entry=(1000, 1030),
        tags=("busy", "light"),
        w=2000,
        h=1200,
    )

    garden = Scene(
        id="garden",
        name="花园",
        kind="outdoor",
        background="assets/bg_garden.png",
        ambience="废铁皮围出来的一小块，土是从废墟推回来的。种的东西活一半死一半，但花是真的开了。",
        props=(
            Prop("flowers", "花", 320, 500, 300, 180, "prop", "gather",
                 text="开得不多，但是真的。没人管，也没人拔草。"),
            Prop("stones", "几块石头", 760, 600, 340, 130, "prop", "sit",
                 text="被坐得发亮。"),
            Prop("watering_can", "洒水壶", 1240, 600, 120, 100, "prop", "look",
                 text="一个漏水的壶，底下一圈湿印。"),
            Prop("door_out", "出口", 690, 870, 260, 100, "door", "enter",
                 target="outdoor", text="钻出铁皮。"),
        ),
        doors={"outdoor": (820, 920)},
        entry=(820, 840),
        tags=("quiet", "open"),
        w=1600,
        h=1000,
    )

    ruins = Scene(
        id="ruins",
        name="废墟",
        kind="outdoor",
        background="assets/bg_ruins.png",
        ambience="楼一栋比一栋高，越往里越静。地上是一层细灰，踩上去没有声音。",
        props=(
            Prop("rubble", "瓦砾堆", 220, 500, 400, 250, "prop", "gather",
                 text="能翻出材料。灰里有东西反光。"),
            Prop("vending", "自动售货机", 920, 460, 200, 290, "prop", "look",
                 text="还没坏。玻璃后面的东西早就不在了。"),
            Prop("deep_gate", "往深处的路口", 1580, 400, 340, 360, "prop", "look",
                 text="一块警示牌，字被刮花了。越往里越暗。"),
            Prop("burnt_car", "烧毁的车", 1180, 900, 340, 190, "prop", "look",
                 text="壳子还在，里面什么都没了。座椅上长了一层灰白的霉。"),
            Prop("crater", "塌陷", 640, 1000, 400, 230, "prop", "look",
                 text="地面塌下去一块，能看见下面的管线。深处有滴水声。"),
            Prop("door_out", "回站里", 970, 1260, 260, 100, "door", "enter",
                 target="outdoor", text="往回走。"),
        ),
        doors={"outdoor": (1100, 1290)},
        entry=(1100, 1230),
        tags=("danger", "quiet", "open"),
        w=2200,
        h=1400,
    )

    return World(
        [outdoor, workshop, canteen, kitchen, storage, hall, garden, ruins, lan_room, mio_room],
        links={
            "outdoor": ("workshop", "canteen", "hall", "storage", "garden", "ruins", "lan_room"),
            "workshop": ("outdoor", "mio_room"),
            "canteen": ("outdoor", "kitchen"),
            "kitchen": ("canteen",),
            "storage": ("outdoor",),
            "hall": ("outdoor",),
            "garden": ("outdoor",),
            "ruins": ("outdoor",),
            "lan_room": ("outdoor",),
            "mio_room": ("workshop",),
        },
    )
