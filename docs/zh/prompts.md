# OAM 运行时提示词

[English](../prompts.md) · 简体中文

## 概述

OAM 将编写好的模型指令保存在下文链接所指的 UTF-8 文本文件中，并随产品一起打包。运行时在导入相关模块时读取这些文件。直接检查和编辑文本文件，然后重启相关进程以加载变更。每次请求的来源文本、参考资料和用户画像自定义指令作为独立的输入数据组装。

## 1. 记忆与用户画像生成

提取调用根据实际路径，决定是否在共享规则之前加入分组前缀、之后加入参考事实后缀，并组合成系统消息。洞察生成与用户画像生成各自使用独立的系统提示词。

| 提示词文件 | 用途 | 运行时使用位置 |
|---|---|---|
| [extraction-prompt.txt](../../src/oam/indexing/extraction-prompt.txt) | 共享的事实提取规则与输出契约 | [extraction.py](../../src/oam/indexing/extraction.py) |
| [extraction-group-prefix.txt](../../src/oam/indexing/extraction-group-prefix.txt) | 确定分组提取的段落归属与输出顺序 | [grouping.py](../../src/oam/indexing/grouping.py) |
| [extraction-reference-suffix.txt](../../src/oam/indexing/extraction-reference-suffix.txt) | 引用既有事实作为参考的规则 | [extraction.py](../../src/oam/indexing/extraction.py) |
| [incremental-insight-prompt.txt](../../src/oam/indexing/incremental-insight-prompt.txt) | 根据目标执行 UPDATE/CREATE，并明确处理每个种子 | [extraction.py](../../src/oam/indexing/extraction.py)、[consolidation.py](../../src/oam/indexing/consolidation.py) |
| [insight-prompt.txt](../../src/oam/indexing/insight-prompt.txt) | 在删除修复时，根据完整的支撑证据重新归纳 | [extraction.py](../../src/oam/indexing/extraction.py)、[repair.py](../../src/oam/governance/repair.py) |
| [profiles/prompt.txt](../../src/oam/profiles/prompt.txt) | 用户画像的默认生成规则和自定义格式规则 | [刷新已存画像](../../src/oam/profiles/worker.py)、[自定义生成](../../src/oam/profiles/generation.py) |

提取和洞察提示词加载时保留全部原始空白字符。画像提示词加载时移除末尾换行。主动修改索引提示词时需要更新索引配方。画像提示词文本用于判断刷新输入是否发生变化。

## 2. 读取资源

[来源索引 Worker](../../src/oam/workers/index_source.py)加载这些提示词以生成文档摘要并完成视觉提取。加载时移除末尾换行。

| 提示词文件 | 用途 |
|---|---|
| [abstract-prompt.txt](../../src/oam/resources/abstract-prompt.txt) | 总结有引用的文档段落 |
| [pdf-vision-system-prompt.txt](../../src/oam/resources/pdf-vision-system-prompt.txt) | 读取 PDF 物理页上的详细文字和表格 |
| [pdf-vision-user-prompt.txt](../../src/oam/resources/pdf-vision-user-prompt.txt) | 通过占位符 `{page_number}` 指定物理页 |
| [image-vision-system-prompt.txt](../../src/oam/resources/image-vision-system-prompt.txt) | 读取图片细节及可选的区域信息 |
| [image-vision-user-prompt.txt](../../src/oam/resources/image-vision-user-prompt.txt) | 请求图片提取结果 |

## 3. 查询规划与部署探针

可选的规划召回使用查询规划模板。部署预检使用固定的探针指令和重排查询。加载时移除末尾换行。

| 提示词文件 | 用途 | 运行时使用位置 |
|---|---|---|
| [planner-prompt.txt](../../src/oam/api/planner-prompt.txt) | 生成检索查询，`{max_queries}` 由搜索配置提供 | [search.py](../../src/oam/api/search.py) |
| [preflight-chat-prompt.txt](../../src/oam/models/preflight-chat-prompt.txt) | 检查 Chat 是否输出可见的 JSON 正文 | [client.py](../../src/oam/models/client.py) |
| [preflight-vision-prompt.txt](../../src/oam/models/preflight-vision-prompt.txt) | 检查模型是否识别探针图片 | [client.py](../../src/oam/models/client.py) |
| [preflight-rerank-query.txt](../../src/oam/models/preflight-rerank-query.txt) | 根据固定的探针文档检查重排得分 | [client.py](../../src/oam/models/client.py) |
