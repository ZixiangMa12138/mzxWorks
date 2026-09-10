# 秋千猫（myPet）

[English README](README_EN.md)

秋千猫是一个以原始荡秋千 GIF 为基础制作的透明桌面宠物项目。仓库同时提供：

| 版本 | 用途 | 入口 |
| --- | --- | --- |
| Codex 原生宠物 | 供 Codex/ChatGPT Desktop 的宠物选择器加载 | `dist/swing-pet/` |
| 独立桌面宠物 | 完整交互、亲密度与状态提示 | `start-standalone.sh` |

原生宠物只负责 Codex 的标准精灵动画；点击、拖拽、跳下秋千、亲密度、锁链和钥匙等扩展交互由独立桌面版实现。

## 快速开始

以下命令均在仓库根目录执行；不需要也不应修改源码中的路径。

```bash
git clone <your-repository-url> myPet
cd myPet
python3 -m pip install -r requirements.txt
```

### 不安装 Codex：启动独立宠物

```bash
./start-standalone.sh --no-codex
```

这会以前台方式启动，适合首次排错；按 `Ctrl+C`、按 `Esc` 或右键菜单中的“退出”可关闭。

常用参数：

```bash
./start-standalone.sh --scale 1.4
./start-standalone.sh --initial-affection 12
./start-standalone.sh --diagnose
```

`--initial-affection 12` 仅用于快速测试低亲密度、爆炸和解锁流程。生产使用请省略该参数。

需要后台且防重复启动时，使用：

```bash
python3 src/autostart_pet.py
```

运行中的 PID、锁和日志位于 `runtime/`；它们都是本机临时状态，不应提交到 Git。独立版启动后的进程显示名为 **PetCat**：`ps -eo pid,comm,args | rg PetCat` 可查看它。

### 安装桌面快捷方式

```bash
./install-desktop.sh
```

安装器会根据 `xdg-user-dir DESKTOP` 创建桌面入口，并在 `$XDG_DATA_HOME/applications`（默认 `~/.local/share/applications`）创建应用菜单入口。Desktop Entry 规范要求可执行文件路径为绝对路径，因此安装后的 `.desktop` 文件会包含当前克隆位置；项目移动或重新克隆后，重新运行此安装器即可更新它。模板 `desktop/swing-pet.desktop.in` 本身不含个人路径。

## 与 Codex 绑定联动

独立桌面版默认启用 `CodexBridge`。它只读取本机 Codex App Server/会话数据，不请求网络，并把状态显示为“工作中”“思考中”或“空闲中”。优先级如下：

1. `runtime/pet-state.json` 的人工覆盖（由 `petctl.py` 写入）；
2. 本机 `codex app-server proxy`；
3. 本机 `~/.codex/sessions/` 下最新会话 JSONL 的增量读取；
4. 空闲。

正常联动启动：

```bash
./start-standalone.sh
```

手动验证状态而无需真的运行任务：

```bash
python3 src/petctl.py working
python3 src/petctl.py waiting
python3 src/petctl.py success
python3 src/petctl.py error
python3 src/petctl.py auto
```

最后一条 `auto` 会删除人工覆盖，恢复自动检测。

若希望 Codex 会话开始或恢复时自动拉起独立宠物，可在你自己的 Codex Hook 配置中执行以下命令（将 `<PROJECT_ROOT>` 替换为本机仓库的绝对路径）：

```text
python3 <PROJECT_ROOT>/src/autostart_pet.py
```

该启动器会立即返回 JSON Hook 结果，并用 PID 文件锁避免与桌面快捷方式重复启动。Hook 的配置位置、触发事件和信任流程会随 Codex 版本及组织策略变化，请在 Codex 的 `/hooks` 界面中添加或确认此命令并完成信任。官方说明中，桌面版可在宠物设置或 `/pet` 中显示宠物；Codex CLI 可通过 `/pets` 或 `/pet` 选择宠物。[官方宠物文档](https://learn.chatgpt.com/zh-Hans/docs/pets)

> 独立版与 Codex 原生宠物是两个窗口/运行时。绑定只同步独立版的状态文字与摆动速度；不能把独立版的自定义交互注入 Codex 原生精灵窗口。

### 安装 Codex 原生宠物

先在需要时重新构建，然后安装：

```bash
python3 src/build_pet.py
./install-local.sh
```

安装器把 `pet.json` 与 `spritesheet.webp` 复制到 `${CODEX_HOME:-$HOME/.codex}/pets/swing-pet/`。重启 Codex 后，在宠物选择器中选择它并使用 `/pet`（或应用中的 Show pet）显示。官方自定义上传格式为透明 PNG/WebP 精灵图，固定为 1536×1872；本仓库的 v2 本地包保留了自己的 8×11 图集协议和清单，请不要混用两种安装方式。

## 环境与必需内容

运行独立版需要：

- Linux、Python 3.10+、GTK 3 与可用的图形会话（X11 或支持透明窗口的 Wayland 合成器）；
- PyGObject/GTK 的系统包；Ubuntu/Debian 示例：`sudo apt install python3-gi gir1.2-gtk-3.0`；
- `requirements.txt` 中的 Pillow、NumPy、SciPy（构建和图像处理需要）；
- `assets/runtime/` 内的 `swing-interactive.png`、`standing-transparent.png`、`jump-down.png`、`jump-up.png`（运行独立版需要）；
- Codex 仅在需要自动状态联动或原生宠物时才是必需项。

在没有合成器、远程纯终端或无 `DISPLAY`/Wayland 会话中，独立窗口无法显示；仍可使用 `--diagnose` 检查素材是否可读。

## 目录结构

```text
myPet/
├── assets/
│   ├── source/                 # 用户提供的 GIF 与站立参考素材
│   ├── runtime/                # 构建出的透明 APNG/PNG 交互素材
│   └── icons/                  # 从最低点摆动帧生成的应用图标
├── desktop/                    # 路径占位符形式的 Desktop Entry 模板
├── dist/swing-pet/             # 可安装的 Codex v2 本地宠物包
├── runtime/                    # 本机 PID、锁、日志、人工状态（Git 忽略）
├── src/                        # 运行时、桥接器和构建工具
├── tests/                      # 状态、拖拽、亲密度与解锁的单元测试
├── pet.config.json             # 图集 id、尺寸和源周期配置
├── requirements.txt            # Python 构建依赖
├── start-standalone.sh         # 前台独立启动
├── install-desktop.sh          # 安装快捷方式
└── install-local.sh            # 安装 Codex 本地包
```

`qa/` 是可再生的视觉检查产物。它可能包含本机绝对路径或较大的预览文件，默认不会作为发布必需文件提交。

## 素材、绘制与动画是如何生成的

| 内容 | 方式 | 相关实现 |
| --- | --- | --- |
| 秋千主体与脸部 | 使用 `assets/source/` 中的原始用户素材；构建时只做抠图、统一裁切、缩放和透明化 | `build_pet.py`、`build_interaction_assets.py` |
| 站立、跳下、跳回 | 由站立参考图与固定秋千分层后生成；跳回是跳下序列的精确时间反向，保证端点吻合 | `build_interaction_assets.py` |
| 摆动 | 从原 GIF 提取闭合周期；所有帧共用坐标框，秋千顶端固定 | `build_pet.py` |
| 应用图标 | 裁切独立摆动素材的最低点帧并放大至 512px | `build_desktop_icon.py` |
| 爱心 | 无额外图片素材，使用 Cairo Bézier 曲线实时绘制、上升并淡出 | `HeartEffect` |
| 爆炸、铁链、锁头、钥匙 | 无额外位图，使用 Cairo 直线、虚线、圆弧、矩形与星形实时绘制；钥匙插入、旋转和锁梁弹开均由时间插值驱动 | `LockdownOverlay` |
| 文本框与低亲密度红色 | GTK 标签/CSS 绘制文本；Pillow 对运行时帧进行色调映射 | `standalone_pet.py` |

因此除 `assets/source/` 的用户原始素材外，大多数动画和特效均可由代码重新生成。请在公开仓库前确认你拥有原始 GIF、参考图和人物表情素材的发布权。

## 交互规则

- 单击宠物：亲密度 `+1`，头部附近出现可叠加的红色爱心；上限为 1000。
- 拖拽宠物：可跨显示器移动。左右拖动时秋千向反方向滞后，上下拖动处于最低点；鼠标停止移动但仍按住时会平滑恢复摆动。
- 悬停静止 1 秒：宠物在最低点跳到秋千旁；离开后跳回秋千。
- 拖拽：每完整秒亲密度 `-5`，最低降到 5；落地站立时每秒 `-1`，同样最低到 5。
- 普通摆动 5 秒无点击/拖拽：亲密度 `-1`，可继续降到 0。低于 10 时逐级变红；5 以下摆动会持续加速。
- 亲密度为 0：爆炸后进入锁链封锁。将随机出现的金色钥匙拖到中央锁孔，钥匙会插入、转动、打开锁梁，锁链消失，亲密度恢复到 100。

## 开发与重新构建

不要直接编辑最终 APNG/WebP。修改源素材、裁切策略或图集配置后：

```bash
python3 src/build_pet.py
python3 src/build_interaction_assets.py
python3 src/build_desktop_icon.py
python3 -m unittest discover -s tests -v
```

主要源码入口：

- `src/standalone_pet.py`：GTK 窗口、状态机、鼠标事件、效果层与动画调度。
- `src/codex_bridge.py`：本地 App Server 优先、会话日志回退和状态优先级。
- `src/autostart_pet.py`：锁文件与 `/proc` 校验，确保重复启动不会出现多个宠物；后台启动时同样将进程名设为 `PetCat`。
- `src/build_pet.py`：清理透明背景、提取闭合摆动周期、生成 Codex v2 图集。
- `src/build_interaction_assets.py`：站立素材清理、秋千/角色分层和跳跃过渡。

源码在关键的坐标系、时间插值、图层拆分和本地 Codex 回退处都包含注释与 docstring。新增行为时，请保持“渲染定时器只读状态、输入事件更新状态、效果层独立窗口”的分层，以避免拖拽和动画互相抢占。

## Git 提交建议

提交源码、原始素材（确认授权后）、构建配置、已验证的运行时素材和 `dist/swing-pet/`。不要提交 `runtime/`、Python 缓存或机器相关 QA JSON；根目录 `.gitignore` 已覆盖这些本机生成物。安装后的桌面入口和个人 Codex Hook 属于用户配置，也不应加入仓库。

## 排错

```bash
./start-standalone.sh --diagnose
python3 -m unittest discover -s tests -v
```

若 Codex 未运行，独立宠物仍可正常工作，只会显示空闲状态；使用 `--no-codex` 可完全关闭桥接线程。若宠物不显示，请先确认当前图形会话有 `DISPLAY` 或 Wayland 环境变量，并检查 `runtime/standalone-pet.log`。
