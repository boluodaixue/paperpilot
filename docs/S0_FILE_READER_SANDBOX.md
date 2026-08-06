# S0 本地文件读取沙箱实施记录

## 状态

已完成（2026-08-28）。S0 只收紧 Research `file_reader` 的授权、路径和内容边界；没有开始 S1，也没有改变同质 Research AgentGraph、Research Workflow、fork policy、递归上限、checkpointer、Markdown Memory Store、Chat Store 或 N6 可选 Red/Blue。

## 已实现

- `FileReaderTool` 默认 deny-all；产品装配不再存在 `allowed_base_dir=None` 的无限制模式。没有可信运行范围时，模型 schema 不暴露该工具，伪造调用仍会被安全拒绝。
- 可信 Runtime 通过异步上下文绑定命名虚拟根。研究开始、恢复和 Web 流式确认只授权当前 managed Memory；没有 Memory、legacy Memory 或缺少 Memory Store 权限时均失败关闭。
- 工具输入固定为虚拟 `root` 与相对 POSIX `path`。模型只能看到 `memory/...` 或受控调用方提供的 `upload/...`，不会得到机器绝对路径。
- 授权根和目标路径使用真实路径归属与文件身份复核；绝对路径、空组件、`.`、`..`、Windows drive/UNC/ADS、保留名、symlink、junction、reparse point、邻接目录前缀欺骗和读取期间 TOCTOU 替换均拒绝。
- 从同一个 OS 文件描述符读取有界字节快照，并在返回前复核根、路径组件和文件身份。拒绝目录、非普通文件、超大文件和不支持的扩展名。
- 文本严格按 UTF-8/UTF-8 BOM 解码；Markdown/TXT、CSV、JSON、PDF 和 DOCX 均有输出上限，CSV/PDF/DOCX 另有行、页、段落或解压上限。
- FileReader 证据只信任工具已经验证的结构化结果路径与正文，不再把模型传入的原始文件参数写进 evidence。沙箱拒绝属于永久错误，不会通过重试扩大权限。
- CLI、Web、评测和 demo 共用安全装配；demo 只在单次调用期间绑定其临时上传目录。

S0 没有新增上传 API。`upload` 虚拟根只是给已有受控上传流程或可信调用方使用的工具契约；普通研究运行目前只绑定当前 managed Memory。

## 安全语义

```text
ResearchRuntime(memory_id)
  -> 校验 managed Memory 描述与真实 Vault 归属
  -> 绑定 memory 虚拟根（仅本次 async 上下文）
  -> AgentGraph 动态提供 file_reader schema
  -> FileReader 从单一描述符读取有界快照
  -> 返回 memory/<relative-path> + 结构化内容
```

并发研究使用 `ContextVar` 隔离各自授权根；授权映射进入上下文时即冻结，调用方之后修改原字典不会改变已授予范围。该设计只解决读取授权，不引入 S1 的持久化 checkpoint/Runtime Registry，也不实现 S2 的单一 Vault Writer。

## 验收

- S0 专项：`43 passed, 1 skipped`；跳过项是当前 Windows 环境没有创建最终文件 symlink 的权限，目录 junction 与授权根替换仍已覆盖。
- W5 时间夹具修复 + S0 联合回归：`74 passed, 1 skipped`。W5 生产实现未改动；测试改为把 Memory 创建时间固定到既有 `STAMP`，消除提交当天中午后必然失败的时间依赖。
- 仓库全量（包含 N1–N6、W0–W6 与 S0）：`493 passed, 1 skipped, 1 warning`。
- 固定离线 `memory_wiki` 评测：`5/5 passed`，`pass_rate = 1.0`。
- warning 是既有 `StarletteDeprecationWarning`；未作为 S0 范围处理。

## 明确未做

- 未引入 `AsyncSqliteSaver`、Runtime Registry、TTL/outbox 或任何 S1 行为；
- 未引入持久写入队列、单一 Vault Writer、journal、staging 发布或 S2 行为；
- 未移动、删除或重写 legacy Memory；
- 未增加 FTS5、向量库、跨 Memory 检索或派生知识真相源；
- 未增加 Repository、Service、索引、Agent 角色或自动写入行为。
