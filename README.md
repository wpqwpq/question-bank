# Question Bank · A-Level / IGCSE 题库

覆盖 **A-Level** 与 **IGCSE / GCSE** 两类考试、**Edexcel（培生）/ AQA / CIE（剑桥国际）** 三个考试局的题目资源库。

本仓库**只装工具与规范**（Schema、切割/渲染/校验脚本、打标台）；题目数据主体位于 `data/`，**不在本仓库内**，按需另行打包分发。设计文档（`docs/`）为内部资料，同样不入库。

## 覆盖范围

| 维度 | 取值 |
| --- | --- |
| 考试局 | Edexcel (Pearson) · AQA · CIE (Cambridge International) |
| 资格 | A-Level · IGCSE · GCSE |
| 学科 | Physics（起步，其余学科可扩展） |
| 场次 | 各考试局历年各考季 |

> 完整界定见内部文档（本地 `docs/01-项目范围.md`，不入库）。

## 仓库结构

```
Question Bank/
├─ data/            题目数据主体（本地目录，已 gitignore，不入库·另行分发）
│  └─ taxonomy/     例外：标签词表随仓库同步
├─ schema/          元数据 JSON Schema
├─ scripts/         切割 / 渲染 / 校验 / 索引等工具
├─ workbench/       打标台（FastAPI + React）
└─ README.md
```

## 三条约定

1. 一切题目数据放在 `data/`，**目录路径本身就是分类**（考试局 / 资格 / 学科 / 场次）；该目录已在 `.gitignore` 中排除，**不随仓库上传**。
2. **一道题一个文件**，文件头（front-matter）携带结构化元数据，正文为题干。
3. 图片、PDF 等二进制素材的 **Git LFS 规则**保留在 `.gitattributes`，供需要把数据自行入库的场景使用。

## 许可

仓库内的代码、Schema 与标签配置供学习参考。试卷原文（题干、图、PDF）版权归 Pearson / AQA / Cambridge International 各考试局所有，**不包含在本仓库中**，使用相关真题请遵守各考试局的版权政策。
