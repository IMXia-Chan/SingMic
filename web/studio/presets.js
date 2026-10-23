// 组合模板库 —— 一条 = 一份完整的风格说明(流派 + 情绪 + 乐器 + 人声处理 + 节奏 + 空间)。
//
// ★ 为什么写在代码里而不是 HTML:面板里只放一个「打开模板库」的按钮,列表由这里渲染。
//   以后加模板只改这个文件,不用碰 markup。
// ★ brief 是**真的会发给平台**的那段字(百炼上限 2000 字,我们这边限 1000),
//   所以每条都控制在 150 字以内 —— 写得越长平台越容易抓不到重点,不是越专业越好。
// ★ quick:1 = 面板上直接露出来的「常用」,其余在库里。

export const PRESET_CATS = [
  ['all', '全部'],
  ['pop', '流行'],
  ['rnb', 'R&B / Soul'],
  ['rap', '说唱'],
  ['electronic', '电子'],
  ['rock', '摇滚'],
  ['folk', '民谣'],
  ['guofeng', '国风'],
  ['jazz', '爵士 / 蓝调'],
  ['lofi', 'Lo-fi / 氛围'],
  ['cinema', '影视 / 配乐'],
  ['world', '世界音乐'],
  ['retro', '复古 / 老歌'],
];

export const PRESETS = [
  // ---------------- 流行 ----------------
  { id: 'pop-anthem', name: '流行 · 大副歌', cat: 'pop', bpm: 104, quick: 1,
    brief: '华语流行, 104 bpm, 钢琴打底 + 合成器 pad + 电吉他扫弦, 鼓组进入副歌全场, 副歌前收一拍留白, 人声贴脸、副歌叠三层和声, 混响适中、宽声场, 明亮、有推动感' },
  { id: 'pop-acoustic', name: '流行 · 木吉他', cat: 'pop', bpm: 92,
    brief: '流行民谣, 92 bpm, 木吉他指弹 + 轻鼓 + 贝斯, 副歌加弦乐和音, 人声干净、气声少, 混响小、像小房间, 温暖、真诚' },
  { id: 'pop-edm', name: '流行电子 · 电台感', cat: 'pop', bpm: 118,
    brief: '流行电子, 118 bpm, 合成器 pluck + 电钢琴 + 四踩底鼓, 副歌前 build-up 抽出底鼓, 人声硬修音、叠双轨, 侧链 pad、混响短, 都市、明亮、适合车载' },
  { id: 'pop-ballad-piano', name: '流行 · 钢琴抒情', cat: 'pop', bpm: 70,
    brief: '华语抒情, 70 bpm, C 大调, 钢琴为主 + 弦乐铺底 + 尼龙吉他, 主歌只有钢琴, 副歌进鼓组和弦乐, 人声贴脸、尾音带颤, 混响大、留白多, 克制、心碎' },
  { id: 'pop-dance', name: '流行舞曲', cat: 'pop', bpm: 124,
    brief: '流行舞曲, 124 bpm, four-on-the-floor 四踩底鼓, 合成器 stabs + 弹跳贝斯, 副歌全开、人声切片做效果, 侧链压缩、明亮, 甜、上瘾' },
  { id: 'pop-gospel', name: '流行 · 福音色彩', cat: 'pop', bpm: 86,
    brief: 'gospel pop, 86 bpm, 风琴 + 钢琴 + 大和声团 + 实录鼓, 副歌和声层层叠加, 主唱即兴 ad-lib, 混响宽、像教堂, 明亮、有希望' },

  // ---------------- R&B / Soul ----------------
  { id: 'neo-soul-82', name: '都市 Neo-Soul', cat: 'rnb', bpm: 82, quick: 1,
    brief: 'neo-soul / R&B, 82 bpm, 降E大调, 4/4 略带摇摆(swing 12%), Rhodes 电钢 + 指弹贝斯 + 刷鼓, 副歌加弦乐垫, 人声贴脸、气声多、双轨叠唱, 短混响 + 一点磁带饱和, 夜晚、克制的失落' },
  { id: 'rnb-slowjam', name: 'Slow Jam · 慢摇', cat: 'rnb', bpm: 64,
    brief: 'slow jam R&B, 64 bpm, 电钢琴 + 合成贝斯 + 电子鼓点, half-time 半速鼓, 人声很近、耳边气声, 加短延迟和叠唱, 混响湿, 性感、缠绵' },
  { id: 'rnb-alt', name: '另类 R&B', cat: 'rnb', bpm: 88,
    brief: 'alternative R&B, 88 bpm, 稀疏鼓组 + 反拍贝斯 + 失谐合成器, 大量留白, 人声做双层、一层过电话音, 混响长、空间空旷, 冷、疏离' },
  { id: 'rnb-funk', name: '放克 R&B', cat: 'rnb', bpm: 102,
    brief: 'funk R&B, 102 bpm, 放克吉他切音 + 指弹贝斯 + 实录鼓, 铜管点缀, 人声干脆、副歌叠和声, 房间感、干声为主, 有劲、适合跟着点头' },
  { id: 'rnb-quiet', name: 'Quiet Storm', cat: 'rnb', bpm: 72,
    brief: 'quiet storm, 72 bpm, 电钢 + 萨克斯 + 柔和弦乐, 鼓很轻, 人声像在耳边说, 混响大、低频厚, 温柔、深夜电台' },
  { id: 'rnb-trapsoul', name: 'Trap Soul', cat: 'rnb', bpm: 75,
    brief: 'trap soul, 75 bpm, 808 低音 + hi-hat 滚奏 + 电钢琴, 半速律动, 人声轻微 autotune、双轨, 混响短、808 撑满低频, 暗、迷离' },

  // ---------------- 说唱 ----------------
  { id: 'rap-trap', name: 'Trap · 夜路', cat: 'rap', bpm: 140, quick: 1,
    brief: 'trap / hip-hop, 140 bpm, 半速鼓(half-time), 808 低音为主, hi-hat 三连滚, 主歌说唱、副歌旋律化, 人声压得干、叠一层低八度, 808 和踩锤对齐, 暗色调、夜路、压迫感' },
  { id: 'rap-boom', name: 'Boom Bap · 老派', cat: 'rap', bpm: 90,
    brief: 'boom bap, 90 bpm, 采样味鼓组 + 黑胶底噪 + 立式贝斯, 人声干、不加混响, 采样切片做 hook, 老派、纽约地下' },
  { id: 'rap-drill', name: 'Drill', cat: 'rap', bpm: 142,
    brief: 'drill, 142 bpm, 滑音 808 + 稀疏鼓点 + 弦乐采样, 人声压得紧、带口音, 副歌重复短句, 混响短, 冷、有威胁感' },
  { id: 'rap-melodic', name: '旋律说唱', cat: 'rap', bpm: 128,
    brief: 'melodic rap, 128 bpm, 温暖合成器 + 808 + 轻鼓, 主歌说唱、副歌整段唱, 人声自动修音、叠和声, 混响适中, 上口、漂' },
  { id: 'rap-zh', name: '中文说唱 · 叙事', cat: 'rap', bpm: 88,
    brief: '中文说唱, 88 bpm, 钢琴 + 弦乐采样 + 中速鼓组, 四四拍直拍, 主歌叙事说唱、咬字清晰, 副歌旋律化, 人声靠前、混响小, 真实、有故事感' },
  { id: 'rap-phonk', name: 'Phonk', cat: 'rap', bpm: 145,
    brief: 'phonk, 145 bpm, 808 cowbell 主音 + 重底鼓 + 失真 808 贝斯, 采样人声切片, 人声低沉、加失真, 混响小, 暗、压迫、驾车' },

  // ---------------- 电子 ----------------
  { id: 'edm-house', name: 'Melodic House', cat: 'electronic', bpm: 122, quick: 1,
    brief: 'melodic house, 122 bpm, A 小调, four-on-the-floor 四踩底鼓, 副歌前 build-up 收掉底鼓, Drop 进主合成器 riff + 侧链 pad, 女声气声切片, 长混响 + 乒乓延迟, 明亮、开阔' },
  { id: 'edm-futurebass', name: 'Future Bass', cat: 'electronic', bpm: 150,
    brief: 'future bass, 150 bpm, 半速鼓 + 超级锯齿波和弦, 副歌 Drop 用切碎人声做和弦, 大量侧链、混响大, 甜、青春、上扬' },
  { id: 'edm-synthwave', name: 'Synthwave', cat: 'electronic', bpm: 100,
    brief: 'synthwave, 100 bpm, 模拟合成器 pad + 琶音 + 电子鼓, 副歌加入失真 lead, 加一点磁带抖晃, 混响宽、有 80 年代感, 复古、夜色' },
  { id: 'edm-dnb', name: 'Drum & Bass', cat: 'electronic', bpm: 174,
    brief: 'liquid drum & bass, 174 bpm, 琶音鼓组 + 深沉 sub 贝斯, 女声切片做氛围, 人声混响长, 速度快、流动感' },
  { id: 'edm-techno', name: 'Techno', cat: 'electronic', bpm: 132,
    brief: 'techno, 132 bpm, 四踩底鼓 + 循环合成器 stab + 工业感噪音, 很少人声, 只有一句处理过的短语反复, 混响长、空间机械, 冷、推进' },
  { id: 'edm-lofi-house', name: 'Lo-fi House', cat: 'electronic', bpm: 118,
    brief: 'lo-fi house, 118 bpm, 闷鼓组 + 采样味和弦 + 黑胶底噪, 人声只做一句气声采样, 混响湿、高频收掉, 慵懒、有毛边' },
  { id: 'edm-chillstep', name: 'Chillstep', cat: 'electronic', bpm: 140,
    brief: 'chillstep, 140 bpm, 半速鼓 + 深沉 sub + 空灵 pad, 人声做长混响、像从远处传来, 大量留白, 安静、漂浮' },

  // ---------------- 摇滚 ----------------
  { id: 'rock-band', name: '摇滚乐队', cat: 'rock', bpm: 132, quick: 1,
    brief: '摇滚, 132 bpm, 失真吉他双轨 + 过载贝斯 + 实录鼓, 主歌鼓偏干、副歌全开, 人声稍脏、带一点过载, 桥段掉到干净吉他后炸回来, 现场感、房间混响, 有冲劲' },
  { id: 'rock-punk', name: '朋克', cat: 'rock', bpm: 168,
    brief: 'punk rock, 168 bpm, 快速鼓点 + power chord + 失真贝斯, 人声喊、带一点破音, 几乎不加混响, 直给、躁' },
  { id: 'rock-post', name: '后摇 / 后摇滚', cat: 'rock', bpm: 96,
    brief: 'post-rock, 96 bpm, 清音吉他琶音开头, 逐层加入贝斯、鼓、弦乐, 高潮全部失真叠加, 人声只在远处和声或无, 长混响、空间大, 从安静到爆发' },
  { id: 'rock-alt', name: '另类摇滚', cat: 'rock', bpm: 118,
    brief: 'alternative rock, 118 bpm, 干净与失真吉他交替 + 驱动鼓组, 主歌克制、副歌打开, 人声靠前、略干, 混响适中, 焦躁、有张力' },
  { id: 'rock-ballad', name: '摇滚情歌', cat: 'rock', bpm: 76,
    brief: 'rock ballad, 76 bpm, 钢琴与清音吉他开头, 副歌进全乐队 + 弦乐, 吉他 solo 在桥段, 人声沙哑、情绪上来, 混响大, 深情、壮' },

  // ---------------- 民谣 ----------------
  { id: 'folk-guitar', name: '民谣 · 木吉他', cat: 'folk', bpm: 88, quick: 1,
    brief: '民谣, 88 bpm, 木吉他指弹 + 口琴 + 轻鼓与贝斯, 段落从头到尾基本不变, 人声干净、像在房间唱, 混响小, 朴实、有故事' },
  { id: 'folk-indie', name: '独立民谣', cat: 'folk', bpm: 76,
    brief: 'indie folk, 76 bpm, 双木吉他 + 班卓 + 轻手鼓 + 弦乐点缀, 多人和声, 人声略毛糙, 房间混响, 温暖、有尘土感' },
  { id: 'folk-city', name: '城市唱作', cat: 'folk', bpm: 84,
    brief: '城市民谣唱作, 84 bpm, 电钢琴 + 木吉他 + 轻鼓, 副歌加弦乐和声, 人声贴近麦克风、气声多, 混响小, 平静、自省' },
  { id: 'folk-trad', name: '传统民谣', cat: 'folk', bpm: 68,
    brief: 'traditional folk, 68 bpm, 木吉他 + 手风琴 + 提琴, 偏慢的三拍子, 多人合唱副歌, 人声朴素, 混响适中, 怀旧、乡愁' },
  { id: 'folk-loops', name: '循环民谣', cat: 'folk', bpm: 94,
    brief: 'acoustic loop, 94 bpm, 木吉他打板 + 循环采样 + 轻电子鼓, 结构靠叠加一层层长起来, 人声干净、加一点和声, 房间感, 清新、有生命力' },

  // ---------------- 国风 ----------------
  { id: 'guofeng-classic', name: '古风 · 民乐', cat: 'guofeng', bpm: 76, quick: 1,
    brief: '古风 / 中国风, 76 bpm, 五声音阶, 古筝 + 笛子 + 琵琶 + 弦乐铺底, 前奏古筝独奏, 副歌进民乐鼓, 人声清亮、尾音带颤, 混响空、类似大殿, 凄美、宿命感' },
  { id: 'guofeng-modern', name: '国风 · 现代融合', cat: 'guofeng', bpm: 96,
    brief: '现代国风, 96 bpm, 古筝与电子鼓组混搭 + 合成器 pad, 副歌进 trap 半速鼓, 人声清亮、叠和声, 混响宽, 古今交错、有仙气' },
  { id: 'guofeng-xianxia', name: '仙侠', cat: 'guofeng', bpm: 72,
    brief: '仙侠配乐, 72 bpm, 箫 + 古琴 + 弦乐 + 定音鼓, 开头只有箫, 高潮全奏, 人声极轻的女声吟唱, 混响极长、空间空旷, 缥缈、苍茫' },
  { id: 'guofeng-folk-song', name: '民族风', cat: 'guofeng', bpm: 84,
    brief: '民族风, 84 bpm, 马头琴 + 手鼓 + 弦乐, 节奏有跳跃感, 人声高亢、带装饰音, 现场感, 辽阔、豪放' },
  { id: 'guofeng-wuxia', name: '武侠', cat: 'guofeng', bpm: 108,
    brief: '武侠风, 108 bpm, 二胡 + 笛子 + 琵琶 + 鼓组, 节奏有行进感, 副歌全员齐奏, 人声低沉有力, 混响适中, 江湖气、悲壮' },

  // ---------------- 爵士 / 蓝调 ----------------
  { id: 'jazz-trio', name: '爵士三重奏', cat: 'jazz', bpm: 96,
    brief: '爵士, 96 bpm, swing 摇摆 30%, 钢琴三重奏(钢琴 + 拨弦贝斯 + 刷鼓), 钢琴即兴 solo, 人声懒散、字句拖后, 房间混响、像小酒吧, 慵懒、深夜' },
  { id: 'jazz-bossa', name: 'Bossa Nova', cat: 'jazz', bpm: 92,
    brief: 'bossa nova, 92 bpm, 尼龙吉他切分 + 轻推后半拍 + 沙锤, 贝斯简单, 人声轻、气声多, 混响小、近场, 温柔、午后' },
  { id: 'jazz-blues', name: '蓝调', cat: 'jazz', bpm: 72,
    brief: 'blues, 72 bpm, 12 小节进行, 电吉他过载 + 口琴 + 立式钢琴 + 慢鼓, 人声沙、带哭腔, 房间混响, 苦、有劲' },
  { id: 'jazz-swing', name: '大乐队 Swing', cat: 'jazz', bpm: 138,
    brief: 'swing big band, 138 bpm, 铜管组 + 钢琴 + 走音贝斯 + 摇摆鼓, 副歌铜管齐奏, 人声明亮、字句利落, 大厅混响, 欢快、有排场' },
  { id: 'jazz-smooth', name: 'Smooth Jazz', cat: 'jazz', bpm: 84,
    brief: 'smooth jazz, 84 bpm, 电钢琴 + 萨克斯主奏 + 柔和鼓组, 副歌萨克斯 solo, 人声柔和、贴耳, 混响适中, 松弛、适合傍晚' },

  // ---------------- Lo-fi / 氛围 ----------------
  { id: 'lofi-loop', name: 'Lo-fi 循环', cat: 'lofi', bpm: 75, quick: 1,
    brief: 'lo-fi / chillhop, 75 bpm, 采样味电钢 + 低沉贝斯 + 轻鼓, 鼓不过门、稍微摇摆, 加黑胶底噪和磁带抖晃, 人声只做气声哼唱或不要人声, 混响大、低频闷, 适合循环' },
  { id: 'ambient-drift', name: '氛围 Ambient', cat: 'lofi', bpm: 60,
    brief: 'ambient, 60 bpm, 长音 pad + 空灵钢琴 + 远处的噪音, 几乎没有鼓, 人声只做无词吟唱、很轻, 混响极长, 安静、漂浮' },
  { id: 'lofi-study', name: '自习 Lo-fi', cat: 'lofi', bpm: 82,
    brief: 'study lo-fi, 82 bpm, 闷鼓 + 电钢和弦循环 + 雨声采样, 结构几乎不变, 不要人声, 混响湿、高频收掉, 平静、不抢注意力' },
  { id: 'lofi-bedroom', name: '卧室流行', cat: 'lofi', bpm: 86,
    brief: 'bedroom pop, 86 bpm, 清音吉他 + 轻合成器 + 松鼓, 人声贴耳、气声多、像在房间录的, 故意留一点毛边和底噪, 混响小, 私密、真实' },
  { id: 'lofi-jazzhop', name: 'Jazzhop', cat: 'lofi', bpm: 80,
    brief: 'jazzhop, 80 bpm, 爵士钢琴采样 + 闷鼓 + 黑胶底噪, 贝斯简单, 不要人声或只要一句哼唱, 混响湿, 松弛、有灰尘感' },

  // ---------------- 影视 / 配乐 ----------------
  { id: 'cinema-epic', name: '影视 · 史诗', cat: 'cinema', bpm: 96, quick: 1,
    brief: '影视配乐, 96 bpm, D 小调, 弦乐 + 钢琴 + 少量电子脉冲, 开头单音后逐层加声部, 中段抽空只留呼吸, 高潮全奏 + 军鼓滚奏, 长混响、宽声场, 悲壮、史诗感' },
  { id: 'cinema-tense', name: '悬疑 / 紧张', cat: 'cinema', bpm: 120,
    brief: '悬疑配乐, 120 bpm, 低音提琴持续音 + 金属打击 + 反拍脉冲, 逐渐加层, 不要人声, 混响干、距离近, 不安、压迫' },
  { id: 'cinema-warm', name: '温情 / 治愈', cat: 'cinema', bpm: 72,
    brief: '温情配乐, 72 bpm, 钢琴 + 弦乐 + 木吉他, 旋律简单重复, 副歌进弦乐, 不要人声, 混响适中, 温暖、有光' },
  { id: 'cinema-chase', name: '追逐 / 动作', cat: 'cinema', bpm: 150,
    brief: '动作配乐, 150 bpm, 持续十六分音符弦乐 + 电子鼓 + 铜管重音, 节奏不断推进, 不要人声, 混响小、干、近, 紧迫、心跳' },
  { id: 'cinema-doc', name: '纪录片 / 叙述', cat: 'cinema', bpm: 88,
    brief: '纪录片配乐, 88 bpm, 钢琴 + 轻弦乐 + 吉他, 结构克制、留出叙述空间, 不要人声, 混响自然, 平静、有分量' },

  // ---------------- 世界音乐 ----------------
  { id: 'world-latin', name: '拉丁', cat: 'world', bpm: 108,
    brief: 'latin pop, 108 bpm, 尼龙吉他 + 康加鼓 + 铜管 + 钢琴 montuno, 副歌全员齐奏, 人声明亮、叠和声, 混响适中, 热烈、有舞感' },
  { id: 'world-reggae', name: '雷鬼', cat: 'world', bpm: 76,
    brief: 'reggae, 76 bpm, 反拍吉他切音 + 弹性贝斯 + 一放鼓, 加一点延迟, 人声放松、叠低八度, 混响适中, 松弛、太阳下' },
  { id: 'world-afro', name: 'Afrobeats', cat: 'world', bpm: 104,
    brief: 'afrobeats, 104 bpm, 多层手鼓 + 弹跳贝斯 + 吉他切音, 节奏切分明显, 人声明亮、副歌重复短句, 混响小, 热、有生命力' },
  { id: 'world-celtic', name: '凯尔特', cat: 'world', bpm: 112,
    brief: 'celtic folk, 112 bpm, 提琴 + 锡笛 + 手鼓 + 木吉他, 旋律有舞曲感, 多人合唱, 混响自然, 辽阔、乡土' },
  { id: 'world-middle', name: '中东风', cat: 'world', bpm: 94,
    brief: 'middle eastern, 94 bpm, 乌德琴 + 卡农琴 + 达布卡鼓, 旋律带微分音装饰, 人声高亢、带转音, 混响宽, 神秘、古老' },

  // ---------------- 复古 / 老歌 ----------------
  { id: 'retro-citypop', name: 'City Pop', cat: 'retro', bpm: 108, quick: 1,
    brief: 'city pop, 108 bpm, 放克贝斯 + 合唱效果电吉他 + 80 年代合成器 + 电钢琴, 实录鼓组, 副歌加铜管, 人声明亮、叠和声, 混响适中, 夏日、都市夜景' },
  { id: 'retro-80s', name: '80 年代', cat: 'retro', bpm: 116,
    brief: '80s pop, 116 bpm, gated 鼓 + 合成器 pad + 失真吉他点缀, 副歌合成器 lead, 人声叠多层、加长延迟, 混响大, 复古、霓虹' },
  { id: 'retro-motown', name: 'Motown', cat: 'retro', bpm: 120,
    brief: 'motown / 60s soul, 120 bpm, 走音贝斯 + 铜管组 + 铃鼓 + 钢琴, 副歌合唱, 人声清亮、带应答, 房间混响, 欢快、有排场' },
  { id: 'retro-trot', name: '老歌 · 抒情慢板', cat: 'retro', bpm: 66,
    brief: '年代感抒情歌, 66 bpm, 弦乐 + 钢琴 + 木吉他, 前奏弦乐起, 副歌加鼓, 人声宽、尾音长、带颤, 混响大, 怀旧、深情' },
  { id: 'retro-disco', name: 'Disco', cat: 'retro', bpm: 122,
    brief: 'disco, 122 bpm, 四踩底鼓 + 反拍贝斯 + 弦乐 stabs + 清音吉他, 副歌弦乐齐奏, 人声高、叠和声, 混响适中, 热闹、舞池' },
];
