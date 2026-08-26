# 变更日志

本文件记录 DeskGuardian 面向用户和贡献者的重要变化。格式参考 Keep a Changelog，版本号遵循语义化版本思路。

## [Unreleased]

### Added

- CLI 与本地 Web 仪表板，用于管理课程、作业、设置和 JSON 导入导出。
- Ollama AI 课表解析，并在不可用、输出无效或置信度不足时降级到规则解析。
- SQLite 本地存储、课程与作业提醒、每日课程简报和跨扫描去重。
- Windows、macOS、Linux 用户级开机自启支持。
- PyInstaller 与 Docker 分发配置。
- 面向公开项目的贡献、安全、隐私、行为准则、治理和第三方依赖文档。

### Security

- 文档明确本地 Web 不应直接暴露到公网，远程 Ollama 配置会改变数据边界。
- 建立私密漏洞报告要求；仓库发布前必须启用 GitHub Private Vulnerability Reporting，或配置真实私密安全渠道。

### Notes

- 首个公开预发布版本计划为 `0.1.0`；正式发布时应把上述条目移动到带发布日期的版本章节。
- 当前默认节次时间仅为可修改示例，不代表任何特定学校作息。

<!--
发布时使用以下格式，并删除空章节：
## [0.1.0] - YYYY-MM-DD
### Added
### Changed
### Deprecated
### Removed
### Fixed
### Security

仓库地址确认后，才添加版本比较链接；不要保留 OWNER/REPOSITORY 占位 URL。
-->
