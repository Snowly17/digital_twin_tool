# -*- coding: utf-8 -*-
"""
mock_data/town/waterfront.py

「滨水小镇」展示场景 —— 真实城镇肌理的布局定义。

为什么不继续用字符地图：
    老版 build_town.py 的 30×22 字符图适合"一眼看懂"，但表达力有限
    （一格只能一个字符，没法描述"沿街退线"、"街区里留出院子"）。
    真实城镇是**地块 + 路网**的关系，所以这里改成「矩形地块清单」：
      · 先定路网与水面，再填地块 —— 这是真实规划的顺序，也是好看的关键
      · 每个地块自动按"沿街退线 + 最小间距"排建筑，而不是一格一栋
      · 所有矩形都经过重叠校验（互相压住会直接报错，不靠人眼对齐）

设计依据（都是从代码/资源里量出来的）：
    · 地面 PlaneGeometry(80,80) → 全部对象必须落在 ±40 内
    · 建筑前端先 autoScale 归一化，再按 custom_props.height 改绝对高度
    · road_straight.glb 是 12×12 且不可单向拉伸 → 路网用新增的 'road' 类型
    · 相机默认 (12,10,15)、FOV 40 → 主看点在中部

场景构成：
    一条河自北向南贯穿东侧，主街东西向跨河（有桥）
    北岸：商业街区 + 中央广场 + 住宅；南岸：住宅 + 滨水绿地 + 充电广场 + 能源区
"""
import math

# ============================================================
# 1. 网格与坐标
# ============================================================
COLS = 32
ROWS = 27
PITCH = 2.2                     # 单位：场景单位（≈ 米）

X_MIN = round(-(COLS - 1) / 2.0 * PITCH, 3)
X_MAX = round(+(COLS - 1) / 2.0 * PITCH, 3)
Z_MIN = round(-(ROWS - 1) / 2.0 * PITCH, 3)
Z_MAX = round(+(ROWS - 1) / 2.0 * PITCH, 3)
GROUND_LIMIT = 40.0


def c2x(col):
    return round((col - (COLS - 1) / 2.0) * PITCH, 3)


def r2z(row):
    return round((row - (ROWS - 1) / 2.0) * PITCH, 3)


def rng(a, b):
    return list(range(a, b + 1))


# 整镇居中平移量（由 build_objects() 写入）。
#   ⚠️ 凡是要和"对象最终坐标"比较的地方，按格子算出来的坐标都必须补上这个偏移，
#      否则会出现假偏差（红绿灯就踩过：差出 7 个单位，其实是坐标系没对齐）。
CENTER_OFFSET = [0.0, 0.0]


# ============================================================
# 2. 占用表 —— 所有矩形都往这里登记，冲突立即可见
# ============================================================
class Occupancy:
    def __init__(self):
        self.cells = {}

    def claim(self, c0, c1, r0, r1, name, allow=(), allow_all=False):
        """登记一个矩形；与已有内容冲突时返回冲突清单（不抛错，便于一次看全）

        allow_all=True 用于「贴着路口摆的充电桩」这类故意复用道路格的情况。
        """
        conflicts = []
        if not allow_all:
            for c in rng(c0, c1):
                for r in rng(r0, r1):
                    old = self.cells.get((c, r))
                    if old is not None and old not in allow:
                        conflicts.append((c, r, old))
        if not conflicts:
            for c in rng(c0, c1):
                for r in rng(r0, r1):
                    self.cells[(c, r)] = name
        return conflicts

    def free(self, c, r):
        return (c, r) not in self.cells


# ============================================================
# 3. 路网（先定路）
# ============================================================
# (行/列, 起, 止, 视觉路宽, 中线, 人行道)
# ⚠️ 约定：**一条街在占用表里只占 1 行（或 1 列）**，视觉宽度另外给。
#    这样做的好处：主街两侧都能贴着街边摆东西（真实城镇就是这样），
#    而不是让主街吃掉 2~3 行、把两侧的行都占掉。
H_ROADS = [
    ('r4',  0, 26, 3.0, False, False),   # 北侧次路
    ('r13', 0, 26, 3.6, False, True),    # 滨河路
    ('r15', 0, 26, 3.6, False, True),    # 滨水步道
]
V_ROADS = [
    ('c5',  5, 14, 3.6, False, True),
    ('c11', 0, 4,  3.0, False, False),
    ('c17', 5, 14, 3.6, False, True),
    ('c23', 0, 3,  3.0, False, False),
]
# 主街：占用表里只占 1 行（row=9），视觉宽度 5.4 单独给 —— 这样两侧都能贴街摆桩
MAIN_STREET = dict(row=9, width=5.4, center_line=True, sidewalk=True)
RIVER_ROAD = 'r13'                       # 沿河路（贴它的北侧摆沿河慢充）
RIVER_WALK = 'r15'                       # 滨水步道
WATER = (28, 30, 0, 21)                  # c0,c1,r0,r1 河面
BRIDGE = (28, 30, 8, 10)                 # 桥：主街跨河处

# ============================================================
# 4. 地块（街区）
# ============================================================
# 行号预算（自上而下）：
#   1-3  北岸公园      4 北侧次路     5-7  商业街区(北)
#   8    ★贴街桩行    9 主街         10   ★贴街桩行
#   11-12 住宅街区(南) 13 滨河路      14   ★贴街桩行(沿河)
#   15   滨水步道      16-17 滨水绿地/住宅
#   18-21 南侧公共设施 22 边界留白    23-25 最南（充电广场/能源区）
DISTRICTS = [
    # 北岸：公园按 c11 / c23 两条纵向路分三段，避免缺格；街区各自只占 1 行
    dict(name='北岸绿地W',  c0=5,  c1=10, r0=2,  r1=3,  use='park'),
    dict(name='北岸公园',   c0=12, c1=22, r0=2,  r1=3,  use='park'),
    dict(name='北岸绿地E',  c0=24, c1=26, r0=2,  r1=3,  use='park'),
    dict(name='商业街区A',  c0=0,  c1=4,  r0=6,  r1=6,  use='commercial', height=(4.5, 6.0)),
    dict(name='商业街区B',  c0=6,  c1=10, r0=6,  r1=6,  use='commercial', height=(4.5, 6.0)),
    dict(name='商业街区C',  c0=12, c1=16, r0=6,  r1=6,  use='commercial', height=(5.0, 6.0)),
    dict(name='中央广场',   c0=18, c1=22, r0=6,  r1=6,  use='plaza'),
    dict(name='商业街区D',  c0=24, c1=26, r0=6,  r1=6,  use='commercial', height=(4.5, 6.0)),
    dict(name='商业街区E',  c0=12, c1=16, r0=11, r1=11, use='commercial', height=(4.0, 5.5)),
    dict(name='商业街区F',  c0=18, c1=22, r0=11, r1=11, use='commercial', height=(4.0, 5.5)),
    dict(name='商业街区G',  c0=24, c1=26, r0=11, r1=11, use='commercial', height=(4.0, 5.5)),
    dict(name='住宅街区W',  c0=0,  c1=4,  r0=11, r1=11, use='residential', height=(2.6, 3.4)),
    dict(name='住宅街区E',  c0=6,  c1=10, r0=11, r1=11, use='residential', height=(2.6, 3.4)),
    # 南岸
    dict(name='滨水绿地',   c0=0,  c1=26, r0=16, r1=17, use='park'),
    dict(name='南绿地',     c0=8,  c1=16, r0=19, r1=22, use='park'),
    dict(name='能源区储能', c0=18, c1=20, r0=19, r1=22, use='energy'),
    dict(name='能源区光伏', c0=22, c1=26, r0=19, r1=22, use='solar'),
    dict(name='充电广场',   c0=0,  c1=7,  r0=19, r1=22, use='charging'),
    dict(name='南绿地2',    c0=8,  c1=16, r0=24, r1=25, use='park'),
    dict(name='能源区储能S', c0=18, c1=20, r0=24, r1=25, use='energy'),
    dict(name='能源区光伏S', c0=22, c1=26, r0=24, r1=25, use='solar'),
]

# 沿街充电带：位置由几何计算（贴街外侧 0.6），不占格
#   (街, 侧, 起列, 止列, 起点序号)
CHARGING_RUNS = [
    # 主街北侧：贴商业区（真实城镇里充电桩就集中在商业/办公一侧）
    ('main',  'north', 0,  4,  1),     # 5 桩
    ('main',  'north', 12, 16, 6),     # 5 桩
    # 主街南侧：只留一小段，不做满
    ('main',  'south', 6,  9,  11),    # 4 桩
    # 沿河路：景区慢充，少量
    ('river', 'south', 14, 16, 15),    # 3 桩
    # 充电广场停车位（这儿是"专门充电的地方"）
    ('lot',   'grid',  0,  7,  18),    # 4 桩
]

# 人行道：**独立成条**（不再只画在路面里），贴合主街与沿河路两侧
#   (街, 侧, 起列, 止列, 宽)
SIDEWALK_RUNS = [
    ('main',  'north', 0, 26, 1.4),
    ('main',  'south', 0, 26, 1.4),
    ('river', 'north', 12, 20, 1.2),
    ('river', 'south', 12, 20, 1.2),
]

# 路上车辆：贴右侧通行，数量少（多了会挡住充电桩）
#   (街, 车道, 沿街坐标)
CARS = [
    ('main',  'east', -20.0),
    ('main',  'east', -4.0),
    ('main',  'east', 12.0),
    ('main',  'west', 24.0),
    ('river', 'east', 8.0),
]

# 🔥 模型朝向修正：car.glb 的**车长在 Z 轴**（1.81 宽 × 1.18 高 × 4.22 长），
#    而主街/沿河路都是沿 X 走的。不补这 90°，车就会**横在马路中间**
#    （用户实测反馈"汽车是拉伸的"，其实根因是朝向错了）。
#    换模型时如果车长在 X 轴，把这里改成 0 即可。
MODEL_FORWARD_OFFSET = math.pi / 2.0

# ============================================================
# 5. 校验 + 构建占用表
# ============================================================
def build_occupancy():
    occ = Occupancy()
    errs = []

    # 5.1 水面
    c0, c1, r0, r1 = WATER
    errs += [('水面越界', c, r) for c, r, _ in occ.claim(c0, c1, r0, r1, '水面')]

    # 5.2 道路（允许压水面：主街跨河处就是桥；允许与其它道路交叉：那就是路口）
    #     主街单独处理：它在占用表里也只占 1 行，视觉宽度由 MAIN_STREET['width'] 给
    road_items = list(H_ROADS) + list(V_ROADS)
    road_items.append(('r%d' % MAIN_STREET['row'], 0, COLS - 1,
                       MAIN_STREET['width'], MAIN_STREET['center_line'],
                       MAIN_STREET['sidewalk']))
    for name, a, b, w, cl, sw in road_items:
        if name.startswith('r'):
            row = int(name[1:])
            errs += [('道路压占', c, r, old)
                     for c, r, old in occ.claim(a, b, row, row, '道路',
                                                allow=('水面', '道路'))]
        else:
            col = int(name[1:])
            errs += [('道路压占', c, r, old)
                     for c, r, old in occ.claim(col, col, a, b, '道路',
                                                allow=('水面', '道路'))]

    # 5.3 地块
    for d in DISTRICTS:
        errs += [('地块压占：%s' % d['name'], c, r)
                 for c, r, _old in occ.claim(d['c0'], d['c1'], d['r0'], d['r1'], d['name'])]

    # 5.4 充电桩位置由几何计算得出，不参与占格校验，但要校验：
    #     · 不落在水面/街区里（沿街摆放）
    #     · 全部在地面 ±40 内
    for x, z, cell, sid in charger_positions():
        if abs(x) > GROUND_LIMIT or abs(z) > GROUND_LIMIT:
            errs.append(('充电桩越界', sid, x, z))
        cellname = occ.cells.get((round((x / PITCH) + (COLS - 1) / 2.0),
                                  round((z / PITCH) + (ROWS - 1) / 2.0)))
        if cellname in ('水面',) or (cellname or '').startswith(('商业', '住宅')):
            errs.append(('充电桩落在%s上' % cellname, sid, x, z))

    return occ, errs


# ============================================================
# 4b. 充电桩位置（几何计算）
# ============================================================
def _road_row(name):
    for nm, a, b, w, cl, sw in H_ROADS:
        if nm == name:
            return int(nm[1:]), w
    return None, None


def charger_positions():
    """沿街充电桩的 (x, z, cell, station_id)

    规则（像真实规划那样算，而不是手摆）：
      · 贴在街道**外侧**摆：偏移 = 路宽/2 + 0.6（留出人行道）
      · 沿街按列均匀铺开，桩都落在给定的列区间内，天然避开纵向道路路口
      · 'lot' 那种是在充电广场内部按网格摆，不贴街
    返回的第三项是桩型：C 快充 / U 超充 / S 慢充
    """
    out = []
    main_row = MAIN_STREET['row']
    main_w = MAIN_STREET['width']
    river_row, river_w = _road_row(RIVER_ROAD)

    for street, side, c0, c1, start in CHARGING_RUNS:
        if street == 'main':
            row, width = main_row, main_w
        elif street == 'river':
            row, width = river_row, river_w
        else:                                   # lot：广场内网格
            row, width = None, None

        cols = rng(c0, c1)
        if street == 'lot':
            # 充电广场内的停车位：固定 4 列 × 2 排 = 8 个慢充
            xs = [c2x(c) for c in cols[:4]]
            for j, zrow in enumerate((24, 25)):
                for i, x in enumerate(xs):
                    sid = 'S%02d' % (start + j * 4 + i)
                    out.append((round(x, 3), round(r2z(zrow), 3), 'S', sid))
            continue

        sign = -1 if side == 'north' else 1
        z = round(r2z(row) + sign * (width / 2.0 + 0.6), 3)
        if len(cols) == 1:
            xs = [c2x(cols[0])]
        else:
            x0, x1 = c2x(cols[0]), c2x(cols[-1])
            span = x1 - x0
            xs = [round(x0 + span * i / (len(cols) - 1), 3) for i in range(len(cols))]
        # 桩型：商业区（主街北侧）快充/超充，住宅区（南侧）快充，沿河慢充
        cell = 'S' if street == 'river' else ('U' if c0 in (5, 8) and side == 'north' else 'C')
        for i, x in enumerate(xs):
            out.append((x, z, cell, '%s%02d' % (cell, start + i)))
    return out


def _street_axis(street):
    """返回 (行, 路宽, 是横向道路?)"""
    if street == 'main':
        return MAIN_STREET['row'], MAIN_STREET['width'], True
    row, width = _road_row(RIVER_ROAD)
    return row, width, True


def sidewalk_positions():
    """人行道独立成条的 (x, z, length, width, rot)：贴街道两侧铺"""
    out = []
    for street, side, c0, c1, sw in SIDEWALK_RUNS:
        row, rw, _horiz = _street_axis(street)
        sign = -1 if side == 'north' else 1
        # 人行道中心 = 车行道边缘 + 自身半宽
        z = round(r2z(row) + sign * (rw / 2.0 + sw / 2.0), 3)
        x0, x1 = c2x(c0), c2x(c1)
        out.append(dict(x=(x0 + x1) / 2.0, z=z,
                        length=abs(x1 - x0) + PITCH, width=sw, rot=0.0))
    return out


def traffic_light_positions():
    """交叉口四个角的红绿灯 (x, z)

    🔥 只给**主路口**（主街 × 第一条纵向路）配信号灯。
       真实小城镇不会每个路口都装 4 盏灯；装两个路口就是 8 盏黄杆子，
       画面里全是灯（用户反馈"红绿灯太多了"）。
       想给更多路口配灯，把 primary_only 改成 False 即可——四角会自动铺开。

    用 centered=False 取路口中心，这样在**居中之前**就把四角算出来；
    整镇居中时这些灯会和其它对象一起被平移，最终仍落在路口四角。
    """
    out = []
    for cx, cz in intersection_points(centered=False, primary_only=True):
        off_x = PITCH * 1.1        # 让到路口外侧
        off_z = PITCH * 1.1
        for dx in (-off_x, off_x):
            for dz in (-off_z, off_z):
                out.append((round(cx + dx, 3), round(cz + dz, 3)))
    return out


def intersection_points(centered=True, primary_only=False):
    """路口中心点 (x, z)：主街与每条纵向道路的交点

    centered=True（默认）返回**与场景对象同一坐标系**的坐标，即已补上
    整镇居中平移量。凡是拿它和对象最终坐标比较的地方都要用默认值，
    否则会像红绿灯那样出现 7 个单位的假偏差（踩过）。
    primary_only=True 时只返回第一个（主）路口。
    """
    main_row = MAIN_STREET['row']
    pts = []
    for name, a, b, _w, _cl, _sw in V_ROADS:
        col = int(name[1:])
        # 只有纵向路的行区间覆盖主街时才算真交叉
        if a <= main_row <= b:
            pts.append((c2x(col), r2z(main_row)))
    if primary_only and pts:
        pts = pts[:1]
    if centered:
        pts = [(round(x - CENTER_OFFSET[0], 3), round(z - CENTER_OFFSET[1], 3))
               for x, z in pts]
    return pts


def car_positions():
    """路上车辆 (x, z, rot, name)

    🔥 朝向必须对齐**街道走向**，不能写死 0/π。
       原因：car.glb 的车长在 Z 轴，而主街是沿 X 走的——写死 rot=0 会让车
       **横在路上**（用户实测反馈"汽车是拉伸的"，其实是方向错了）。
       做法：取该街道自身的方向（road_segments 已算好 rot），
       再按"右侧通行"决定同向还是掉头，并用方向向量的法向做车道偏移。
    """
    main_rot, river_rot = 0.0, 0.0
    river_row = _road_row(RIVER_ROAD)[0]
    for seg in road_segments():
        if seg['center_line']:
            main_rot = seg['rot']
        elif abs(seg['z'] - r2z(river_row)) < 0.01:
            river_rot = seg['rot']
    street_rot = {'main': main_rot, 'river': river_rot}

    out = []
    for street, lane, along in CARS:
        row, rw, _h = _street_axis(street)
        cz = r2z(row)
        base = street_rot.get(street, 0.0)
        # 贴右侧车道：沿行进方向的法向偏移
        off = (rw / 2.0 - 0.9)
        side = 1.0 if lane == 'east' else -1.0
        dx, dz = math.cos(base), math.sin(base)
        nx, nz = -dz, dx          # 法向 = 方向旋转 90°
        x = along * dx + nx * off * side
        z = cz + along * dz + nz * off * side
        # rot 对齐街道走向，再补上"模型自身朝向"的 90° 修正
        #   （车模型车长在 Z，街道沿 X，不补就横在路上）
        rot = base + MODEL_FORWARD_OFFSET
        if lane == 'west':
            rot += math.pi
        out.append((round(x, 3), round(z, 3), round(rot, 4),
                    '车辆-%s-%s' % (street, lane)))
    return out


def plan_lines(occ):
    """把占用表画成平面图"""
    style = {'水面': '~', '道路': '-', '中央广场': 'P',
             '能源区储能': 'E', '能源区光伏': 'F',
             '能源区储能S': 'E', '能源区光伏S': 'F',
             '充电广场': 'C', '南绿地': 'T', '南绿地2': 'T', '滨水绿地': 'T'}
    grid = [['·'] * COLS for _ in range(ROWS)]
    for (c, r), name in occ.cells.items():
        if name in style:
            grid[r][c] = style[name]
        elif name.endswith('绿地') or name.endswith('公园'):
            grid[r][c] = 'T'
        elif name.startswith('商业'):
            grid[r][c] = 'M'
        elif name.startswith('住宅'):
            grid[r][c] = 'H'
        else:
            grid[r][c] = '?'
    # 充电桩位置叠加显示（它不占格，是按几何算出来的）；只在空白格上叠加，
    # 避免把"充电广场"这类地块样式盖掉、造成误读
    for x, z, cell, _sid in charger_positions():
        c = int(round(x / PITCH + (COLS - 1) / 2.0))
        r = int(round(z / PITCH + (ROWS - 1) / 2.0))
        if 0 <= c < COLS and 0 <= r < ROWS and grid[r][c] == '·':
            grid[r][c] = {'C': 'c', 'U': 'U', 'S': 'S'}.get(cell, 'c')
    # 桥单独标出来
    bc0, bc1, br0, br1 = BRIDGE
    for c in rng(bc0, bc1):
        for r in rng(br0, br1):
            if grid[r][c] == '~':
                grid[r][c] = '='
    return [''.join(row) for row in grid]


# ============================================================
# 6. 生成场景对象
# ============================================================
ROAD_COLOR = '#3c4148'
SIDEWALK_COLOR = '#767d88'
WATER_COLOR = '#1d4f6e'
PLAZA_COLOR = '#8f939b'

# 用途 → 前端物体类型
USE_TO_TYPE = {'commercial': 'building_tall', 'residential': 'building'}

# ============================================================
# 经纬度反解（给 CSV / 数据库 / API 这些需要地理坐标的出口用）
# ============================================================
# 🔥 与老版 charge town 同一个坑，也同一个解法：
#    app 的 CSV 导入会按 ×111×cos(lat)×30 把经纬度换算成场景坐标，
#    并且按"数据均值"居中；所以这里基准固定在整数锚点，
#    靠**场景坐标本身已经居中**来保证导入落点零偏差。
#    （反过来把基准挪到数据重心上，会让 lon-avg_lon 变成两个几乎相等的
#      7 位小数相减、吃掉有效位，再被 ×3075 放大成米级平移。）
SCALE = 30.0
ANCHOR_LAT = 22.5400
ANCHOR_LON = 114.0500
COS_LAT = math.cos(math.radians(ANCHOR_LAT))
LAT_UNITS_PER_DEG = 111.0 * SCALE
LON_UNITS_PER_DEG = 111.0 * COS_LAT * SCALE


def scene_to_latlon(x, z):
    """场景坐标 → 经纬度（严格反解 app 的换算）"""
    # 🔥 精度必须给足：经纬度每 1e-7 度 ≈ 0.0003 场景单位，而下游会 rounding 到
    #    0.01；只给 7 位会正好卡在舍入刀口上，产生有的点差一格的假偏差（踩过）。
    #    9 位 → 余量 0.03 单位，稳。
    lat = ANCHOR_LAT + z / LAT_UNITS_PER_DEG
    lon = ANCHOR_LON + x / LON_UNITS_PER_DEG
    return round(lat, 9), round(lon, 9)


def _obj(kind, i, otype, name, x, z, **custom):
    cp = {'town': 'waterfront'}
    cp.update(custom)
    return {
        'id': 'wf-%s-%03d' % (kind, i),
        'type': otype,
        'name': name,
        'position': {'x': round(x, 3), 'y': 0.0, 'z': round(z, 3)},
        'rotation': {'x': 0, 'y': 0, 'z': 0},
        'scale': {'x': 1, 'y': 1, 'z': 1},
        'bind_station_id': '',
        'custom_props': cp,
        'utilization': 0.5,
    }


def district_cells(occ, name):
    return sorted([(c, r) for (c, r), v in occ.cells.items() if v == name])


def road_segments():
    """把路网合并成长条：横向一条、纵向一条，而不是一格一个方块

    每条街都是"一条长条"：前端 createRoad 会按 length/width 分开渲染，
    并自动铺中线和人行道。
    """
    segs = []
    items = list(H_ROADS) + [(('r%d' % MAIN_STREET['row']), 0, COLS - 1,
                              MAIN_STREET['width'], MAIN_STREET['center_line'],
                              MAIN_STREET['sidewalk'])]
    for name, a, b, w, cl, sw in items:
        row = int(name[1:])
        x0, x1 = c2x(a), c2x(b)
        segs.append(dict(x=(x0 + x1) / 2.0, z=r2z(row),
                         length=abs(x1 - x0) + PITCH, width=w, rot=0.0,
                         center_line=cl, sidewalk=sw))
    for name, a, b, w, cl, sw in V_ROADS:
        col = int(name[1:])
        z0, z1 = r2z(a), r2z(b)
        segs.append(dict(x=c2x(col), z=(z0 + z1) / 2.0,
                         length=abs(z1 - z0) + PITCH, width=w,
                         rot=round(math.pi / 2.0, 4),
                         center_line=cl, sidewalk=sw))
    return segs


def build_objects():
    """按占用表生成完整对象清单（含实时数据字段）"""
    occ, errs = build_occupancy()
    if errs:
        raise ValueError('布局非法，先修 layout：%s' % errs[:3])

    objs = []
    counter = [0]

    def add(kind, otype, name, x, z, **custom):
        counter[0] += 1
        # 🔥 名字必须全局唯一：否则"8 条路都叫 道路"这种重名会让按名字匹配的
        #    校验脚本互相覆盖、产生假偏差（踩过：CSV 与场景比对差 29 个点）。
        unique = '%s-%03d' % (name, counter[0])
        o = _obj(kind, counter[0], otype, unique, x, z, **custom)
        objs.append(o)
        return o

    # ---------- 6.1 水面 + 广场铺装 ----------
    wc0, wc1, wr0, wr1 = WATER
    add('water', 'water', '城市河道',
        (c2x(wc0) + c2x(wc1)) / 2.0, (r2z(wr0) + r2z(wr1)) / 2.0,
        width=abs(c2x(wc1) - c2x(wc0)) + PITCH,
        length=abs(r2z(wr1) - r2z(wr0)) + PITCH,
        color=WATER_COLOR)
    for d in DISTRICTS:
        if d['use'] == 'plaza':
            add('plaza', 'sidewalk', d['name'],
                (c2x(d['c0']) + c2x(d['c1'])) / 2.0,
                (r2z(d['r0']) + r2z(d['r1'])) / 2.0,
                width=abs(c2x(d['c1']) - c2x(d['c0'])) + PITCH,
                length=abs(r2z(d['r1']) - r2z(d['r0'])) + PITCH,
                color=PLAZA_COLOR, center_line=False)

    # ---------- 6.2 路网 ----------
    for seg in road_segments():
        o = add('road', 'road', '主街' if seg['center_line'] else '道路',
                seg['x'], seg['z'], width=seg['width'], length=seg['length'],
                color=ROAD_COLOR, center_line=seg['center_line'],
                sidewalk=seg['sidewalk'])
        o['rotation']['y'] = round(seg['rot'], 4)

    # ---------- 6.2b 人行道（独立成条） ----------
    for i, sw in enumerate(sidewalk_positions(), start=1):
        o = add('walk', 'sidewalk', '人行道-%02d' % i, sw['x'], sw['z'],
                width=sw['width'], length=sw['length'],
                color=SIDEWALK_COLOR, center_line=False)
        o['rotation']['y'] = round(sw['rot'], 4)

    # ---------- 6.2c 路口红绿灯 ----------
    for i, (x, z) in enumerate(traffic_light_positions(), start=1):
        add('light', 'lamp', '红绿灯-%02d' % i, x, z)

    # ---------- 6.2d 路上车辆 ----------
    for i, (x, z, rot, nm) in enumerate(car_positions(), start=1):
        o = add('car', 'car', '%s-%02d' % (nm, i), x, z)
        o['rotation']['y'] = rot

    # ---------- 6.3 街区内容 ----------
    for d in DISTRICTS:
        cells = district_cells(occ, d['name'])
        if not cells:
            continue
        use = d['use']
        if use in ('commercial', 'residential') and d.get('height'):
            h0, h1 = d['height']
            otype = USE_TO_TYPE[use]
            for k, (c, r) in enumerate(cells, start=1):
                h = round(h0 + (h1 - h0) * ((k * 7) % 5) / 4.0, 2)
                add('bld', otype, '%s-%02d' % (d['name'], k), c2x(c), r2z(r),
                    height=h, floor=max(1, int(h / 1.5)), district=d['name'],
                    use='commercial' if use == 'commercial' else 'residential')
        elif use == 'solar':
            for k, (c, r) in enumerate(cells, start=1):
                if k % 2 == 1:      # 光伏阵列隔一格，留出检修通道
                    add('solar', 'solar_panel_land', '光伏板-%02d' % k, c2x(c), r2z(r))
        elif use == 'energy':
            for k, (c, r) in enumerate(cells, start=1):
                if k % 2 == 1:      # 储能柜隔一格摆，留出巡检通道
                    add('cont', 'container_b', '储能柜-%02d' % k, c2x(c), r2z(r))
        elif use == 'charging':
            continue        # 充电桩由 charger_positions() 统一产生
        else:               # park：每 3 格一棵树，避免糊成一片
            for k, (c, r) in enumerate(cells, start=1):
                if k % 6 == 0:
                    add('tree', 'tree', '景观树-%02d' % k, c2x(c), r2z(r))

    # ---------- 6.4 充电桩 + 站点数据 ----------
    stations = []
    for x, z, cell, sid in charger_positions():
        otype = {'C': 'charger_fast', 'U': 'charger_super', 'S': 'charger_slow'}[cell]
        power = {'C': 120, 'U': 180, 'S': 60}[cell]
        base = {'C': 0.58, 'U': 0.78, 'S': 0.26}[cell]
        idx = int(sid[1:])
        # 分区：超充=能源区；充电广场内（z 很大）=lot；主街北侧=商业区；其余=滨水
        if cell == 'U':
            zone = 'energy'
        elif z > 20:
            zone = 'lot'
        else:
            zone = 'commercial' if z < 0 else 'park'
        util = round(max(0.05, min(0.95, base + 0.12 * math.sin(idx * 1.7))), 3)
        o = add('chg', otype, '充电站-%s' % sid, x, z,
                power=power, zone=zone, source='waterfront')
        o['bind_station_id'] = sid
        o['utilization'] = util
        stations.append(dict(
            station_id=sid, name=o['name'], x=x, z=z,
            utilization=util, power=power,
            available_slots=max(0, int((1 - util) * 8)),
            status=('高负载' if util > 0.85 else ('离线' if util < 0.15 else '在线')),
            zone=zone))

    # ---------- 6.5 把整座镇平移到原点 ----------
    # 🔥 必须做这一步：app 的 CSV/GeoJSON 导入会按**所有要素的均值**把数据居中，
    #    而"场景存档"是按原坐标渲染的。两者要逐点一致，场景坐标的均值就必须是 0。
    #    平移量同时写进 ANCHOR_LAT / ANCHOR_LON，保证经纬度反解与之一致。
    global ANCHOR_LAT, ANCHOR_LON
    mean_x = sum(o['position']['x'] for o in objs) / len(objs)
    mean_z = sum(o['position']['z'] for o in objs) / len(objs)
    CENTER_OFFSET[0] = round(mean_x, 3)
    CENTER_OFFSET[1] = round(mean_z, 3)
    for o in objs:
        o['position']['x'] = round(o['position']['x'] - mean_x, 3)
        o['position']['z'] = round(o['position']['z'] - mean_z, 3)
    for s in stations:
        s['x'] = round(s['x'] - mean_x, 3)
        s['z'] = round(s['z'] - mean_z, 3)
    ANCHOR_LAT = round(ANCHOR_LAT + mean_z / LAT_UNITS_PER_DEG, 9)
    ANCHOR_LON = round(ANCHOR_LON + mean_x / LON_UNITS_PER_DEG, 9)

    return objs, stations, occ


if __name__ == '__main__':
    occ, errs = build_occupancy()
    print('=' * 78)
    print('滨水小镇布局校验')
    print('=' * 78)
    if errs:
        for e in errs[:20]:
            print('  ❌ ' + str(e))
    else:
        print('  ✅ 水面/道路/地块 无重叠、无越界；充电桩全部沿街且落在地面内')

    print('\n  M 商业  H 住宅  P 广场  c 快充  U 超充  S 慢充  E 储能  F 光伏')
    print('  T 绿地  = 桥  - 道路  ~ 水面\n')
    for i, line in enumerate(plan_lines(occ)):
        print('  %2d %s' % (i, line))

    chs = charger_positions()
    kinds = {}
    for _x, _z, cell, _sid in chs:
        kinds[cell] = kinds.get(cell, 0) + 1
    print('\n  充电桩 %d 个 %s' % (len(chs), kinds))
    print('  横向 x %.1f ~ %.1f   纵向 z %.1f ~ %.1f   格距 %.1f'
          % (X_MIN, X_MAX, Z_MIN, Z_MAX, PITCH))
    print('  地面限制 ±%.0f' % GROUND_LIMIT)
    raise SystemExit(1 if errs else 0)
