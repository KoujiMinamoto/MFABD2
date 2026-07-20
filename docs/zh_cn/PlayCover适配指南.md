# PlayCover(iOS) 适配指南(macOS)

在 Apple Silicon Mac 上,MFABD2 可以直接控制 PlayCover 运行的 iOS 版棕色尘埃2,**无需安卓模拟器**。本文说明连接方法、必须修改的游戏设置,以及当前的已知限制。

## 原理

MaaFramework(v5.9.2 起随包附带 `libMaaPlayCoverControlUnit.dylib`)原生支持通过 PlayTools 的 MaaTools 服务控制 PlayCover 应用,MFAAvalonia 亦有对应的连接配置界面。本适配只是在 `interface.json` 中暴露该控制器,并附带一层 `resource/playcover` 资源覆盖,处理 iOS 客户端与安卓客户端的 UI 差异。

## 前置条件

- Apple Silicon Mac(M 系列芯片)
- **fork 版 PlayCover**(内含 MaaTools 的 [hguandl/PlayCover](https://github.com/hguandl/PlayCover/releases),与 MAA 明日方舟 macOS 方案同款;官方主线版 PlayCover 没有 MaaTools,不能用)
- 棕色尘埃2 iOS 版(脱壳 IPA)已安装且能正常游玩,游戏语言切换为简体中文
- PlayCover 图形分辨率保持 1920×1080

## 连接步骤

1. PlayCover 中右键棕色尘埃2 → 设置 → 绕过 → 勾选「MaaTools」,保存后启动游戏;
2. 游戏窗口标题栏末尾会出现 `[127.0.0.1:端口]`(默认 1717);
3. MFABD2 中:控制器选「PlayCover(iOS)」,资源选「PlayCover(iOS)」,连接地址填上一步的地址。

## 关键游戏设置(必改,否则人物不走路)

**游戏内设置 → 将移动方式改为触摸移动**(iOS 端默认为虚拟方向键)。

脚本的走路动作是"长按地面某点"(安卓客户端行为)。iOS 客户端在虚拟方向键模式下按住地面无效,所有依赖走路的任务(地图采集、救赎、周常等)会在移动步骤卡死超时;切换为触摸移动后与安卓行为一致,实测正常。

## 已知限制

- **需手动启动游戏**:PlayCover 控制器不支持 `start_app`/文本输入/按键,请先手动把游戏开到主界面再跑任务,「[全局]启动脚本」建议不勾选;
- 仅支持单点触控(MaaTools 协议限制)。

## 钓鱼适配说明

iOS 客户端钓鱼界面与安卓存在差异,由 `resource/playcover` 覆盖层与 agent 配合处理(均已在 iOS 端实测跑通):

- **入场判定**:iOS 端「卖鱼」钱袋图标(`Sell_ico`)在下竿钓鱼过程中会隐藏,原 `Fishing_Start` 节点在该状态下会卡死。覆盖层改为识别抛竿按钮入场;钱袋可见时先执行一次清包出售,不可见则直接开钓;
- **卖鱼页面标题**:iOS 端点击钱袋打开的页面标题为「钓鱼包」而非「商店」,`SellFish_Shop` 的 OCR 已两者均接受,其余出售流程(一键出售/全选/确认/返回)与安卓一致;
- **Perfect Cast(蓄力抛竿)**:覆盖层将 `Casting_Rod` 改为 Custom 动作 `HoldCastGreen`——按住抛竿按钮蓄力,检测蓄力环变绿瞬间松手,超时 5 秒回退普通抛竿。base(安卓)资源不经过此路径,行为不变;
- **agent 性能优化(全平台受益)**:小游戏进度条分析由每帧 3 次 `run_recognition` RPC 改为 agent 本地 numpy 计算(HSV 阈值与 pipeline 节点一致),单帧分析从 ~0.7s 降至截图耗时(实测 ~0.12s);预测时机已过期时跳过该次点击等待游标折返;小游戏开局最多等待 4 秒让进度条完成渲染(此前靠分析延迟"歪打正着",提速后必须显式等待);
- 卖鱼间隔默认 30→25 条(鱼包容量 30,留余量防止开局未能清包时包满卡死)。

## 实测环境

- macOS 26.x(Apple Silicon)+ fork 版 PlayCover + 游戏 2.29.23(简体中文)
- 已验证:MaaTools 连接/截图/点击/长按走路,地图采集与救赎的地图导航,菜单交互类任务流程,钓鱼全链路(蓄力抛竿/上钩/小游戏/结算/自动卖鱼)
- 待充分回归:其余任务完整跑通率(欢迎更多 Mac 用户反馈)
