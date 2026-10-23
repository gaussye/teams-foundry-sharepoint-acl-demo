# Teams + Foundry + SharePoint：权限隔离 Demo 说明

> **当前状态：规划与只读环境调查，尚未实现或部署本 Demo，也未完成真实 Teams A/B 测试。**
> 本文解释方案，不代表已通过产品兼容性验证，也不授权修改云资源、业务文档或安全策略。

## 1. 这个 Demo 要证明什么

两个人在 Teams 中，向**同一个 Agent、同一个知识库（KB）**问同样的问题：

| 测试用户 | 在 SharePoint 中应有的权限 | Agent 应使用的资料 |
|---|---|---|
| A，财务用户 | 公共库 + 财务库 | 公共资料、财务资料 |
| B，人事用户 | 公共库 + 人事库 | 公共资料、人事资料 |

例如，两人都问：“请总结各部门的预算安排。”

- A 可以得到公共信息和财务库中的预算信息，不能看到人事库中的内部资料。
- B 可以得到公共信息和人事库中的预算信息，不能看到财务库中的内部资料。
- “不能看到”包括正文、文件标题、链接、引用，以及从别人的聊天历史中泄露的信息。

**权限必须来自真实的 SharePoint 授权，不能靠提示词要求模型“别说”，也不能在代码中写死“A 是财务，B 是人事”。**

管理员往往有额外权限，不能直接代替普通用户做“应该看不到”的测试。
受保护的答案计划只在私聊中返回，不把按某个人权限查到的内容公开到群聊或频道。

## 2. SharePoint Indexer：先复印到自己的资料库，再查

可以把 SharePoint 想象成公司的档案室：

**Indexer 像搬运员，定期把选定文件的内容复制、整理到 Azure AI Search 中。以后提问，主要查这份副本。**

```text
准备资料时：
SharePoint 文件
  -> SharePoint Indexer（读取、搬运）
  -> 切分内容；按需生成向量；同步可支持的权限信息
  -> Azure AI Search Index（可检索的资料副本）

用户提问时：
Teams -> 身份验证后端 -> Foundry Agent -> KB
  -> Search Index 按当前用户权限检索
  -> Agent 根据授权片段生成带引用的答案
```

这里有几个容易混淆的词：

| 名词 | 用人话解释 | 不是什么 |
|---|---|---|
| **Indexer，索引器** | 读取来源、处理资料、写入索引的搬运任务 | 不是存放资料的数据库 |
| **Index，索引** | 存放可搜索内容和字段的集合，类似整理好的资料库 | 不是后台搬运任务 |
| **Chunk，片段** | 将长文件切成的小段，方便查到具体内容 | 切小以后不会自动获得原文件权限 |
| **Embedding，向量** | 表示内容含义的一串数字，便于找到“意思相近”的内容 | 不包含“谁有权查看”的安全保证 |
| **ACL，访问控制列表** | 记录哪些用户或组可以访问，例如真实 Entra 用户/组 ID | 不是向量，也不是模型自己判断的部门标签 |

### 复制了内容，不等于复制了权限

一个文件在 SharePoint 中只能由财务组阅读，**不能据此假设它复制到 Search 后仍然只有财务组能查到。**
必须把下面几件事接起来：

1. **入库时带权限。** 配置 ACL 读取和字段映射；如果文件被切成多个 chunk，每一个 chunk 都要继承正确权限，不能只给父文件加权限。
2. **查询时执行权限。** 配置 Search 的权限过滤，并安全传入当前用户的访问令牌，让检索层过滤未授权资料，而不是先把所有资料交给模型。
3. **变化后同步权限。** 文件新增、删除、共享变化、组成员变化，都要有相应处理和复测。

内置 SharePoint indexer 有 ACL 入库能力，但有 API 版本、授权方式和 SharePoint 权限模型的支持边界，**不是完整权限体系的无条件镜像**。[2]

尤其要注意撤权：

- 新版 Preview API 可以在后续成功运行中刷新具有独立权限的文件级 ACL 变化。
- **库、站点、文件夹等上级权限变化，不会自动刷新所有继承它的文件。** 需要显式权限重同步或重置相关文档，再成功运行索引器。[2]
- 用户被移出组，还要考虑组解析、令牌和缓存的传播情况；不能宣称立即生效。
- 即使索引已经更新，旧聊天里保存的答案也不会自动消失，需要另行处理历史内容。

所以，“SharePoint 已撤权”与“检索副本已经完成撤权同步”是两件事。
同步尚未确认时，不应把旧副本当成可靠的最新授权结果。

## 3. Remote SharePoint：提问时带身份去 Microsoft 365 资料库查

**Remote 像带着当前用户的证件去微软管理的 Microsoft 365 资料库查资料，再取回该用户有权查看的相关片段。**
不需要我们自己维护一套 SharePoint 文档和 ACL 的 Search 索引副本。

```text
Teams -> 身份验证后端 -> Foundry Agent
  -> 现有 Azure AI Search 服务中的 KB
  -> Remote SharePoint knowledge source
  -> Microsoft 365 Copilot Retrieval API
  -> Microsoft 365 根据当前用户身份和来源权限返回片段
  -> Agent 生成带引用的答案
```

这里**仍然有 Search 服务和 KB**，只是 SharePoint 内容不需要复制到我们自己的 Search Index。
Microsoft 365 在底层有它自己的搜索、索引和处理机制，不是每问一句就下载、遍历整个站点的所有文件。

这条路线减少了我们维护内容副本和 ACL 副本的工作，但不等于“什么都不用管”：

- **不是即时一致承诺。** 新文件、权限、组成员和服务缓存等变化仍可能有传播过程，需要真实测试。
- **不会自动解决 Teams 身份传递。** 后端仍要拿到真正的 A/B 用户身份，按接口要求换取令牌，再传到 Agent/KB 检索链路；不能所有请求都用管理员身份。
- **不会绕过 Conditional Access。** 如果下游要求补做 MFA 或满足设备等条件，应用必须正确处理登录挑战；不满足时停止检索，不能改用管理员或 app-only 身份兜底。
- **有许可和费用前提。** 按官方当前文档，查询用户需要 Microsoft 365 Copilot 附加许可，或已启用适用的 Retrieval API 按量计费。按量计费还有租户至少保有一个 Copilot 许可等前提，不能理解为“完全不需要 Copilot 许可”。[3]
- **仍有 Preview、格式、查询长度和限流等约束。** 不能把 Remote 描述为无条件成熟、全功能或适合任何生产环境的方案。[3]

“原生权限”指授权由 Microsoft 365 来源服务执行，不代表我们无需核实应用的身份链路、适用策略与实际返回结果。

## 4. 两条路线放在一起看

| 对比项 | SharePoint Indexer / Indexed 路线 | Remote SharePoint 路线 |
|---|---|---|
| 通俗理解 | 先复印到自己的资料库再查 | 带当前用户身份去 Microsoft 365 资料库查 |
| 内容放在哪里 | SharePoint 原文 + 我们维护的 Search 内容/片段副本 | SharePoint/Microsoft 365；不在我们的 Search 中建立这份内容索引副本 |
| 是否保留 Agent 和 KB | 是 | 是 |
| 是否有我们自己的共享文档 Index | 是，可让 A/B 共用一个 Index | 不需要这个文档 Index，但仍使用 Search 服务承载 KB |
| 权限怎么执行 | 先同步支持的 ACL，再在查询时按当前用户过滤 | 来源服务根据当前用户身份执行授权 |
| 谁负责副本权限的新鲜度 | 我们要配置、监控和验证同步及撤权 | 不维护自己的文档/ACL 索引副本，但仍要考虑来源传播和缓存 |
| 身份是否可以省略 | 不能省略本 Demo 的用户身份校验 | 同样不能 |
| 主要前提 | 连接器支持范围、读取权限、ACL 入库、查询过滤、同步机制 | Retrieval API 许可/计费、用户授权、完整身份链路和区域能力 |
| 当前 Demo 的关键问题 | 内置 indexer 的官方 CA 支持限制 | 许可前提尚未确认满足，且 CA/用户令牌端到端行为尚未验证 |

**Indexer 是“自己维护检索副本”，Remote 是“不自己维护这份副本”。两者都必须认真处理权限和身份。**

## 5. Conditional Access 到底是什么

Conditional Access，简称 **CA，条件访问**，可以理解成：

> 即便钥匙或密码正确，进门还得满足条件：例如完成 MFA、使用合规设备，或从允许的位置访问。

它和文件权限不是同一件事：

| 安全检查 | 主要回答什么问题 | 例子 |
|---|---|---|
| 身份认证 | 你是谁 | 你确实是用户 A |
| Conditional Access | 这次访问是否满足进门条件 | 这次登录需要 MFA，或需要合规设备 |
| SharePoint ACL | 进入以后能看哪些文件 | A 能看财务库，但不能看人事库 |

**满足 CA，不会自动获得所有文件权限；有文件权限，也不代表可以跳过 CA。**

### CA 不是某个 SharePoint site 的总开关

CA 是 Entra 租户中的一组策略。管理员可以按用户/组、目标资源、设备、位置、风险等条件设置适用范围，也可以配置排除项。[4]

因此：

- 有一条策略是 **On / 启用**，不等于所有用户的所有请求都被阻断；要看具体匹配范围和要求。
- **Report-only / 仅报告**用于评估策略影响，不强制执行这条策略的访问要求。[5]
- 没有启用 CA 策略，也不能反推“没有 MFA”；MFA 还可能通过 Security Defaults 等机制要求。[6]
- 策略列表不是“只针对当前登录账号的策略列表”，也不能单看策略名称判断谁会受到影响。

本轮只读调查发现了启用的 CA 策略。策略可能涉及 MFA、风险重认证、改密或安全信息注册等要求。
**策略名称不能证明 A/B 被包含还是被排除，也不能证明策略仅针对当前账号。**
实际范围要检查策略配置，必要时结合 What If 和登录日志；本 Demo 尚未完成这项适用性验证。

## 6. “SharePoint Indexer 不支持 CA”到底意味着什么

微软的**内置 SharePoint in Microsoft 365 indexer**文档，在 Security limitations 中明确写道：[1]

> No support for tenants with Microsoft Entra ID Conditional Access enabled.

用人话说：**官方把“启用了 CA 的租户”列在这个特定连接器的支持范围之外。**
所以，当前不能把它作为本租户中“已经确认受支持、可以放心落地”的连接方案。

下面这些结论要分开看：

| 可以确定的事 | 不能据此推出的事 |
|---|---|
| 官方文档对这个内置连接器列出了 CA 限制 | 整个 Azure AI Search、SharePoint、Teams 或 RAG 都不可用 |
| 我们读到了启用的 CA 策略 | 每一次请求都会被这些策略阻断 |
| 内置 indexer 路线存在产品支持问题 | 我们已经实测了索引失败；本 Demo 尚未跑这个实验 |
| 一次请求或一个实验可能成功 | 这个组合就变成官方支持，或撤权和长期运行一定可靠 |
| admin 可以登录并读到资源 | 后台连接器的产品限制被消除了 |

**官方这段说明没有解释具体实现根因。** 不能把“后台程序不能做 MFA，所以必然失败”写成确定原因。
不同认证方式适用的策略并不相同，例如 app-only 不一定受面向用户的 MFA 策略影响；这也不等于 app-only 自动消除了连接器的支持限制。

同理，排除某个账号或让管理员重新登录，不足以认定这个连接器已进入官方支持范围。
**不建议为了跑通 Demo 关闭 CA，也不会擅自修改或排除现有策略。**

Remote 不依赖这个内置 indexer，因此避开了这一连接器依赖；但它仍需遵守自身的认证与 CA 要求。
下游如果返回“需要重新登录/补充验证”的 claims challenge，身份链路必须能正确交回用户处理。
通用 OBO 文档说明了这类处理机制，但不能据此保证 Teams、Foundry、KB 和 Retrieval API 的整条链路已经验证可用。[7]

## 7. 对本 Demo 的影响和当前进度

### 当前意向与边界

- 用户此前表达继续 Indexer/Indexed 方案的意向；**没有改选 Remote**。
- 复用现有 Azure AI Search 服务，不新建 Search 服务；实际资源名称和 endpoint 不在仓库中记录。
- 计划在已选测试站点中使用三个专用 Demo 库与真实安全组，仅放合成文档，不改现有业务库；实际站点地址不在仓库中记录。
- 计划保持同一个 Agent、KB，以及 Indexed 路线下的一个共享内容 Index。
- 这些是规划意向，**不是创建库/组、配置应用授权、兼容性实验或部署的批准**。

### 已经做了什么

使用获准的管理身份完成了只读调查；仓库不记录实际账号、租户 ID 或订阅 ID：

- 确认现有 Search 运行中，并读取了服务规格、托管身份和 Entra 认证配置。
- 读取到现有 Index、KB 等对象数量和配额；没有修改它们，也没有检索业务文档正文。
- 确认测试站点和 Azure 使用同一 Entra 租户。
- 确认租户中有启用的 CA 策略；尚未验证它们对本 Demo 各段访问的适用性。
- 检查了测试用户与租户的许可信息。**目前不能认为 Remote 的许可前提已满足，也不能认为 A/B 已具备全部测试服务权益。** 仓库不记录具体许可分配明细。

管理员仅用于获准的资源操作，不能把管理员令牌代替 A/B 的用户令牌。

### 还没有完成什么

**尚未实现或部署 Demo，尚未创建专用库/组、Demo Index/KB 或应用凭证，尚未进行真实 Teams A/B、缺失/过期令牌、并发隔离、撤权传播测试。**
现有 Search 中的已有资源，不是本 Demo 已部署成功的证据。

目前的关键阻塞是内置 indexer 的 CA 支持限制，不能靠“先假定可用”跳过去。
在不改安全策略的前提下，后续需要确认产品支持情况，或另行批准调整路线：

- 若评估 Remote，需要接受不建本地内容 Index，并核实许可、费用、身份传递和 CA 挑战处理。
- 若坚持自己的共享 Index，可评估**自建真实内容与有效 ACL 同步**：保留 Agent、KB 和 Search，替换内置搬运连接器。但这不是把文档搬到 Blob 就结束，还要负责权限继承、每个 chunk 的 ACL、权限变化、删除、查询过滤和撤权验证，也仍需遵守租户认证策略。

这两项都只是备选评估方向，不代表已经切换方案或获得部署批准。

## 8. 官方参考资料

以下说明应结合当前官方文档和目标租户验证，Preview 能力与限制可能变化。

1. [SharePoint indexer：限制与注意事项，包括 CA 支持限制](https://learn.microsoft.com/en-us/azure/search/search-how-to-index-sharepoint-online#limitations-and-considerations)
2. [SharePoint ACL 入库、chunk 权限映射和权限重同步](https://learn.microsoft.com/en-us/azure/search/search-indexer-sharepoint-access-control-lists)
3. [Remote SharePoint knowledge source：架构、许可、限制与用户授权](https://learn.microsoft.com/en-us/azure/search/agentic-knowledge-source-how-to-sharepoint-remote)
4. [Microsoft Entra Conditional Access 概述](https://learn.microsoft.com/en-us/entra/identity/conditional-access/overview)
5. [Conditional Access 的 Report-only 模式](https://learn.microsoft.com/en-us/entra/identity/conditional-access/concept-conditional-access-report-only)
6. [Security Defaults：包含 MFA 等基础保护](https://learn.microsoft.com/en-us/entra/fundamentals/security-defaults)
7. [OBO 中处理 MFA、Conditional Access 和交互认证挑战](https://learn.microsoft.com/en-us/entra/msal/dotnet/acquiring-tokens/web-apps-apis/on-behalf-of-flow#handling-multi-factor-auth-mfa-conditional-access-and-incremental-consent)
8. [Foundry Agent 连接 KB，并逐请求传递用户授权](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/foundry-iq-connect)
9. [Search 查询时 ACL 执行机制与限制](https://learn.microsoft.com/en-us/azure/search/search-query-access-control-rbac-enforcement)
10. [自建入库时，通过 Search Push API 写入真实 ACL](https://learn.microsoft.com/en-us/azure/search/search-index-access-control-lists-and-rbac-push-api)
