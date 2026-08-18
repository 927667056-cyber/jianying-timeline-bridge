# Jianying Timeline Bridge｜剪映时间线双向桥

[English](README_EN.md) · [兼容范围](COMPATIBILITY.md) · [安全说明](SECURITY.md) · [隐私说明](PRIVACY.md)

将受支持的 DaVinci Resolve / Premiere FCP7 XML 与单行 SRT 转换为可继续拖动剪辑点的剪映专业版工程，并把本工具创建、带完整溯源记录的剪映工程确定性反导为 FCP7 XML 与 SRT。

当前公开版本是严格限定范围的 alpha：遇到未知版本、未知结构、素材身份变化或时间线不一致时立即停止，不会静默跳过、近似转换或覆盖已有工程。

## 核心能力

- `FCP7 XML + 可选 SRT → 剪映专业版 11.2 加密草稿`。
- `受管剪映草稿 → FCP7 XML + 精准单行 SRT`。
- 保留原素材路径、完整素材时长和每段 source in/out，剪辑点可在素材边界内继续向两侧扩展。
- 正向生成后解密回读并逐帧核验；反向生成后重新解析 XML 并比较时间线语义。
- 精确锁定剪映 build 和 `videoeditor.dll` SHA-256；版本变化即停止。
- 同名停止、操作锁、来源证明、事务日志、索引备份和中断恢复。
- 本地运行，不上传素材或草稿。

## 已验证范围

| 项目 | 当前支持 |
|---|---|
| 操作系统 | Windows x64 |
| 剪映专业版 | `11.2.0.14339` |
| DLL SHA-256 | `654371a6dca840f53ae786df43fc345365e54d2189d5572ce7c4658aab23f540` |
| 时间线 | FCP7/xmeml v5、30fps CFR、NDF |
| 轨道 | 单原素材、连续且严格配对的 V1/A1 |
| 片段 | 1×、默认变换、无复杂特效、无间隙和重叠 |
| 素材时长 | XML 与实测一致；或实测仅多 1 个且未被引用的尾帧 |
| 字幕 | 可选精准单行 SRT |
| 反向导出 | 仅限本工具创建并带有效 provenance 的草稿 |

完整边界见 [COMPATIBILITY.md](COMPATIBILITY.md)。

## 安装与配置

前置要求：64 位 Python 3.10+、FFmpeg/FFprobe，以及上表中的精确剪映版本。仓库不包含剪映、`videoeditor.dll`、媒体素材或用户草稿。

最简单的方法是双击 `00_首次配置.bat`，依次选择剪映安装目录、`ffmpeg.exe`、`ffprobe.exe` 和草稿根目录。配置工具只会新建本机的 `software_build/bridge_config.json`，已存在时拒绝覆盖。

也可以在同一个 PowerShell 会话中设置四个环境变量：

```powershell
$env:JYBRIDGE_JIANYING_HOME = 'C:\Path\To\JianyingPro\11.2.0.14339'
$env:JYBRIDGE_FFMPEG = 'C:\Path\To\ffmpeg.exe'
$env:JYBRIDGE_FFPROBE = 'C:\Path\To\ffprobe.exe'
$env:JYBRIDGE_DRAFT_ROOT = 'C:\Path\To\JianyingPro Drafts'
```

完全退出剪映后，在同一个 PowerShell 会话中运行只读自检：

```powershell
python bridge_cli.py doctor
```

使用首次配置工具生成本机配置后，也可以双击 `00_环境自检.bat`，通过后再打开 `启动_剪映时间线双向桥.bat`。

## 命令行示例

先只生成和验证准备稿：

```powershell
python bridge_cli.py to-jianying `
  --xml 'C:\Media\episode.xml' `
  --srt 'C:\Media\episode.srt' `
  --draft-name 'Episode_JY_Test_01' `
  --run-dir 'C:\BridgeRuns\Episode_JY_Test_01'
```

门禁全部通过后，增加 `--deploy` 才会把新工程注册到剪映草稿列表。任何同名目标都会停止，绝不替换。

反向导出：

```powershell
python bridge_cli.py from-jianying `
  --draft 'C:\JianyingPro Drafts\Episode_JY_Test_01' `
  --output-bundle 'C:\BridgeExports\Episode_JY_Test_01'
```

中断事务检查与恢复：

```powershell
python bridge_cli.py recover --report-dir 'C:\BridgeRecovery\Run_01'
```

## Codex Skill

配套 Skill 位于 `integrations/codex/jianying-timeline-bridge`。它只选择正式路线、运行软件门禁并解释回执，不重写或绕过转换引擎。

## 验证证据

内部验收覆盖两条独立长时间线，包括数百组配对音视频片段、数百条单行字幕和超过三万帧，并覆盖“实测素材仅多 1 个未使用尾帧”的兼容路线。两条时间线正反向逐段帧边界均一致。出于隐私和版权原因，精确节目规模、真实媒体、真实草稿、原始报告路径及设备索引均未进入仓库。详情见 [docs/VALIDATION.md](docs/VALIDATION.md)。

## 安全、隐私与免责声明

- 请勿提交真实草稿、`root_meta_info.json`、转换回执或未审查的兼容性包；它们可能包含本机路径和设备信息。
- 本工具通过用户电脑上已安装、且哈希完全匹配的剪映本地组件处理加密草稿；不提供或下载厂商 DLL、密钥或剪映安装文件。
- 本项目为非官方互操作工具，与剪映、CapCut、字节跳动或脸萌科技无隶属、赞助或背书关系。
- Alpha 软件不能替代人工验收。首次用于新节目时，应在剪映和目标 NLE 中打开、保存、重开并抽查剪辑点。

## 许可证

项目采用 Apache License 2.0。所包含的最小 `pyJianYingDraft` 加密接口派生自 pyJianYingDraft 0.3.0，许可和归属说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
