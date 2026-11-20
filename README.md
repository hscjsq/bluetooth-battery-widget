# 蓝牙电量小组件 · Bluetooth Battery Widget

Windows 10/11 任务栏右下角小组件，显示**当前已连接**的蓝牙设备电量。

以 `WS_CHILD` 真子窗口挂到 Explorer 的 `Shell_TrayWnd`，紧贴托盘通知区左侧；背景近乎全透明，直接透出任务栏原生底色。纯 Win32 分层窗口（`UpdateLayeredWindow`）+ Pillow 绘制，不依赖 tkinter / Qt，不需要托盘图标，也不依赖系统通知中心。

## 主要能力

- **只显示在线设备**：数据层以 `DEVPKEY_Device_IsConnected` 为主判据，按 `ContainerId` 把一台物理设备拆出的蓝牙 / HID / AudioEndpoint 多节点取 OR 聚合，辅以 `BluetoothAPIs` 的 `fConnected` 与音频端点 `DeviceState`。电量百分比只是「最后一次上报的缓存值」，断开后不会清零，所以不能拿它当在线依据；没有明确在线证据一律判离线。
- **新设备秒级出现**：真子窗口收不到 `WM_DEVICECHANGE` 广播，因此另建隐藏顶层窗口代收并转发（150ms 防抖）；再加 2s 轻量签名探测（SetupDi 遍历约 6ms）和 10s 全量兜底，三层互不依赖。
- **低电量气泡**：≤10% 时弹一次自绘圆角气泡，每台设备 24h 冷却，跨重启有效（`alert_state.json` 原子写入、损坏自动重建）；只统计在线设备，多台低电合并为一个气泡。
- **任务栏自适应**：位置按 `Shell_TrayWnd` / `TrayNotifyWnd` 实时矩形计算，按任务栏高度垂直居中、按天气组件实测边界水平让位；底色采样任务栏真实颜色后透明度设为 1 直接透出，文字按底色明暗自动选深/浅色。换分辨率、换缩放、换深浅主题都无需配置。
- **交互**：悬停显示完整设备名 / 连接状态 / 电量提示；按住图标左右拖动排序（写入 `device_order`）；超过 3 台显示 `+N` 溢出菜单；两级右键菜单 —— 主菜单只留 5 项高频操作，低频开关收进「设置 ▸」悬停展开的级联子菜单，点一下即生效。
- **图标**：`icons/` 下放透明底白色线稿 PNG / ICO / WebP，程序按 Windows 真实 PnP 类别（而非设备名猜测）匹配；未提供的类型自动回退自绘线稿。深浅色任务栏都适配。

## 运行

源码主体在 `bt-battery-tray/`，该目录下 `bt_widget.py` 与 `icons/` 同级，clone 后可直接跑。

```bash
python -m pip install Pillow
cd bt-battery-tray

python bt_widget.py --selftest     # 自检：读设备、渲染、排序、tooltip，不弹窗
python bt_widget.py                # 常驻运行
```

需要 Python 3.9+ / Windows 10 或 11。`vendor/` 是打包时 `pip install --target vendor pillow` 生成的 Pillow 副本，不入库；源码会优先用系统安装的 Pillow。

## 打包成单文件 exe

```bash
cd bt-battery-tray
python -m PyInstaller --noconfirm --clean --onefile --noconsole ^
  --name BtBatteryWidget --icon app.ico --hidden-import bt_battery bt_widget.py
```

把 `config.json`、`icons/`、`app.ico` 放到 exe 同级目录即可。程序用「exe 所在目录」定位配置和图标，因此可以整文件夹拷到别的机器，无硬编码绝对路径。

## 目录说明

| 路径 | 内容 |
| --- | --- |
| `bt-battery-tray/bt_widget.py` | 主程序：窗口、绘制、动画、菜单、报警、刷新调度 |
| `bt-battery-tray/bt_battery.py` | 数据层：SetupAPI PnP 电量与在线判定、设备类别推断 |
| `bt-battery-tray/config.json` | 默认配置（干净值，无本机坐标与设备残留） |
| `bt-battery-tray/icons/` | 运行时读取的 5 张设备线稿（蓝牙标识用自绘） |
| `bt-battery-tray/*.bat`、`start.vbs` | 启动、自检、装依赖、安装版启动辅助脚本 |
| `图标成品/` | 图标原始稿与预览图 |
| `A+B-*.png` | 开发过程效果截图；部分为已回退的历史版本，仅作记录 |
| `快速开始.txt` | 面向新用户的上手指南 |
| `使用说明.txt` | 完整配置项与实现细节 |
| `设置开机自启.bat` | 一键写入当前用户启动项 |

完整参数见 `使用说明.txt`。配置项也可在右键「设置 ▸」里直接点选，无需手改 JSON。

## 关键配置

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `display_mode` | `taskbar_child` | 真子窗口；可切 `taskbar` 浮窗 / `card` 卡片 |
| `hide_disconnected` | `true` | 断开设备自动从任务栏消失 |
| `quick_check_seconds` | `2` | 轻量在线签名探测周期 |
| `refresh_seconds` | `10` | 全量兜底刷新周期 |
| `alert_threshold` | `10` | 低电提醒阈值（%） |
| `alert_cooldown_hours` | `24` | 同一设备两次报警的最小间隔 |
| `tb_max_items` | `3` | 横条最多显示几台，其余折叠为 `+N` |
| `smooth_transition` / `hover_fade` / `hover_tips` | `true` | 过渡动画、天气悬停淡出、设备悬停提示 |

## 已知限制

- 外来 Win32 子窗口无法接入 Explorer 的 XAML 原生动画时间线，过渡动画为自绘（20ms 帧、ease-out、交叉淡化），不保证与系统动画完全同步。
- 全屏独占窗口（游戏等）会盖住任务栏，此时组件被遮住属正常现象，切回桌面即可看到。
- 仅支持 Windows，依赖 Windows PnP / SetupAPI / `BluetoothAPIs.dll`。
- 未指定开源许可。仓库默认私有：源码注释、说明文件与开发截图里含本机设备名和桌面内容，转公开前需先脱敏。
