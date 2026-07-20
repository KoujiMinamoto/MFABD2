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
- **钓鱼的自动卖鱼不可用**:iOS 端出航后顶栏没有「卖鱼」入口图标(安卓端 `Sell_ico` 位于 roi [802,11,92,74],iOS 端全屏搜索无匹配),`resource/playcover` 覆盖层将 `Fishing_Start` 改为识别抛竿按钮直接进入钓鱼循环,鱼包满请手动出售;
- 仅支持单点触控(MaaTools 协议限制)。

## 实测环境

- macOS 26.x(Apple Silicon)+ fork 版 PlayCover + 游戏 2.29.23(简体中文)
- 已验证:MaaTools 连接/截图(1920×1080)/点击/长按走路,地图采集与救赎的地图导航,菜单交互类任务流程
- 待充分回归:钓鱼小游戏细节、各任务完整跑通率(欢迎更多 Mac 用户反馈)
