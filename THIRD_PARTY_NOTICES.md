# 第三方说明（THIRD PARTY NOTICES）

本文件说明本项目的许可状况与第三方项目的关系。本文不构成法律意见。

## 本项目

本项目以 MIT 许可发布（见 `LICENSE`）。

## SPlayer（参考对象，非代码来源）

- 项目：SPlayer — <https://github.com/SPlayer-Dev/SPlayer>
- 作者：imsyy
- 许可：AGPL-3.0

**本项目未复制、未改编、未逐行翻译 SPlayer 的任何源代码。** 仅参考了其公开可观察的行为：

1. **接口调用方式**：使用哪个 HTTP 端点、参数名与返回结构。这类接口契约本身来自下述 MIT 许可的 API 服务，并非 SPlayer 独创。
2. **歌词合并的功能规则**：把原文、翻译、罗马音按时间轴合并为同时间戳的多行 LRC，容差取 300 毫秒。

其中第 2 点需要特别说明：**本项目为独立实现，且匹配语义与 SPlayer 不同。**
SPlayer 采用顺序双指针贪心（两个指针只能前进，命中即同时前进，否则时间较早者前进）；
本项目采用全局最近优先（在容差窗口内枚举全部候选对，按「时间差、原文时间」排序后一一占用）。
二者在边界情形下结果不同，例如原文时间 `[0, 200]`、译文 `[150]`：SPlayer 把译文配给 0，本项目配给更近的 200。
相关行为由 `tests/test_lyrics.py` 中的用例固定。

本项目与 SPlayer 项目及其作者无隶属或背书关系。

## 网易云音乐 API 服务（用户自行获取，本项目不分发）

- 项目：NeteaseCloudMusicApi Enhanced — <https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced>
- 许可：MIT
- 版权声明：Copyright (c) 2013-2022 Binaryify（上游 NeteaseCloudMusicApi 作者）；现由 MoeFurina 维护

本项目**不包含、不再分发**该服务的任何代码或依赖树。使用者需自行获取，途径包括但不限于：
自行用 `npx` 运行、使用其官方 Docker 镜像 `moefurina/ncm-api`，或复用本机已安装的 SPlayer 内置服务。
使用该服务时请遵守其许可与相关服务条款；该服务默认启用的第三方音源解锁功能与本项目无关，
本项目只使用官方接口，建议将其关闭（见 README）。

## Python 依赖

以下依赖通过包管理器安装，未随本仓库分发，各自遵循其许可：

| 依赖 | 用途 | 许可 |
| --- | --- | --- |
| Flask | 本地 Web 服务 | BSD-3-Clause |
| requests | HTTP 客户端 | Apache-2.0 |
| mutagen | 音频标签读写 | GPL-2.0-or-later |
| pytest | 测试（开发依赖） | MIT |

以上依赖均由使用者通过包管理器自行安装，本仓库不分发其源代码或二进制文件，
因此不影响本项目的 MIT 许可（其中 mutagen 为 GPL-2.0-or-later，但同样未被分发）。
测试还需要系统安装 `ffmpeg`（用于生成测试音频），它同样未被分发。

## 其他

本项目不提供任何音乐内容，不绕过任何数字版权管理措施，仅用于个人合法备份你自己账号有权访问的内容。
使用前请自行确认符合当地法律与相关服务条款。
