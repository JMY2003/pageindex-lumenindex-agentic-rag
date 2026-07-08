# 我做了一个 PageIndex-inspired 的 Agentic RAG 应用：不用向量库，也能做多文档问答

最近我开源了一个项目：**LumenIndex**。

项目地址：

https://github.com/JMY2003/lumenindex-pageindex-inspired-agentic-rag

一句话介绍：**LumenIndex 是一个受 PageIndex 启发的、自托管的 Agentic RAG 文档问答系统。它不依赖向量数据库，而是围绕文档目录、章节结构、页码范围和工具调用来完成检索增强问答。**

如果你关注过 PageIndex、Vectorless RAG、长文档问答、PDF 问答、企业内部知识库，应该会对这个方向比较熟悉：很多文档并不是“切块以后向量召回”就能处理好的。尤其是长 PDF、制度文件、招股书、年报、合同、研究报告这类材料，结构本身往往比局部相似度更重要。

## 为什么想做这个项目

传统 RAG 的常见路线是：切 chunk、算 embedding、进向量库、相似度召回、塞给 LLM。

这条路线很通用，但在长文档场景里经常遇到几个问题：

1. **结构丢失**：模型拿到的是碎片，而不是“这段内容属于哪个章节、处于全文哪个层级”。
2. **引用不稳定**：用户问的是制度、条款、财报指标时，答案需要页码和章节依据，而不仅是语义相似片段。
3. **多文档对比困难**：多个文档一起问时，光靠向量召回很容易混上下文。
4. **工程复杂度增加**：向量库、embedding 模型、chunk 策略、重建索引、版本迁移，都会变成维护成本。

PageIndex 给了我一个很有启发的思路：**能不能先让系统理解文档结构，再让 Agent 按照人类读文档的方式检索？**

比如先看目录，再找相关章节，再读具体页码，最后基于证据回答。

LumenIndex 就是沿着这个方向做的一个完整 Web 应用。

## LumenIndex 做了什么

LumenIndex 不是一个简单 demo，而是按“可以交付给真实用户使用”的目标做的。

目前它支持：

- PDF / DOCX / DOC / Markdown 文档上传
- 多文档检索问答
- 标准问答模式和 ReAct Agent 模式
- ChatGPT 风格的会话历史
- 用户注册、登录、资产隔离
- Admin 管理普通用户和资产
- 上下文接近窗口上限时自动压缩
- OpenAI-compatible API，包括 Qwen / DashScope 这类兼容接口
- Docker Compose 部署
- 文档索引进度、取消、重建索引、缓存复用
- Markdown 完整渲染，包括表格和数学公式

前端做成了偏 Apple liquid glass 的工作台风格：左侧是历史和文档，中间是 Chat，右侧可以弹出文档 outline。你可以上传文件、把文件拖进聊天、选择多个文档一起问，也可以回到之前的会话继续带上下文追问。

## 核心思路：不是先向量化，而是先理解结构

LumenIndex 的检索流程大致是：

1. 对文档建立层级结构索引；
2. 保留每个节点对应的页码范围、标题、摘要和章节路径；
3. Agent 先查看 outline；
4. 再用 focused search 找相关章节；
5. 最后只读取紧凑的证据页；
6. 答案必须基于工具返回的证据，并给出文档名、页码、章节引用。

这和很多“直接召回 chunk”的系统不太一样。

我更希望它像一个谨慎的文档分析助手：先翻目录，再定位章节，再打开对应页，而不是一上来就把全文切成碎片让模型猜。

## 和原始 PageIndex 的关系

LumenIndex 是 **PageIndex-inspired**，但不是原始 PageIndex 代码的简单套壳。

它借鉴的是 PageIndex 的核心思想：

- 文档结构优先；
- 推理式检索；
- 紧页范围读取；
- 尽量减少对向量数据库的依赖；
- 基于证据回答。

但 LumenIndex 做了很多 Web 系统层面的扩展：

- FastAPI 后端；
- SQLite + JSON/cache 镜像；
- 多用户登录和权限隔离；
- 多文档会话；
- SSE 流式 Agent trace；
- Pydantic 校验 LLM 结构化输出；
- 上下文压缩；
- Docker Compose 部署；
- 可视化文档列表、outline、聊天历史。

所以你可以把它理解成：**一个面向真实产品形态的 PageIndex-inspired Agentic RAG App**。

## 为什么我觉得 Vectorless RAG 值得继续探索

我并不是说向量库不好。

向量检索适合很多开放语义检索场景，而且成熟、稳定、生态完整。

但在文档问答里，尤其是严肃文档、企业文档、长 PDF 场景下，结构信息非常关键。很多问题不是“哪段话和 query 最像”，而是：

- 这个问题属于哪个章节？
- 这个条款的上下文是什么？
- 它在第几页？
- 多份文档之间有没有冲突？
- 回答能不能追溯到具体证据？

这些问题天然更适合“结构 + 推理 + 工具调用”的方式。

LumenIndex 现在的路线是：先把结构索引做好，再让 Agent 在结构上行动。

## 一些工程细节

项目里我比较重视几个点：

**1. 结构化输出必须校验**

LLM 返回 JSON 不能直接信。LumenIndex 里模型输出的节点选择、工具参数、文档结构等都会经过 Pydantic 校验，非法结果会被过滤。

**2. 会话不是一问一答，而是真正的 Chat**

每个 conversation 会保存完整消息和文档选择。用户回到历史对话后可以继续追问。上下文太长时会自动压缩并归档。

**3. 用户资产隔离**

普通用户只能看到自己的文档和聊天记录。Admin 可以管理普通用户资产，但不能管理其他 admin 的资产。

**4. 索引任务可观测**

上传、reindex、失败、取消都有进度状态。索引在子进程里跑，FastAPI 主进程负责监督状态、取消和缓存写入。

**5. 前端不是临时 demo**

我尽量把它做成一个实际工作台：会话、文档、outline、上传、设置、管理页都在一个产品体验里。

## 适合谁试用

如果你正在做：

- 企业内部文档问答；
- 长 PDF / 年报 / 合同 / 制度文件分析；
- PageIndex 相关实验；
- 无向量库 RAG；
- OpenAI-compatible 私有模型接入；
- Agentic document retrieval；

可以试试这个项目。

项目地址：

https://github.com/JMY2003/lumenindex-pageindex-inspired-agentic-rag

也欢迎提 issue、star 或者 fork。现在项目还在快速迭代，我后面比较想继续做的方向包括：

- 更低成本的 TOC 推理索引；
- 更好的 PDF 版面理解；
- 文档对比问答；
- Benchmark 示例；
- 和更多 PageIndex-style 工作流兼容。

如果你也在探索 PageIndex / Vectorless RAG / Agentic RAG，欢迎交流。

