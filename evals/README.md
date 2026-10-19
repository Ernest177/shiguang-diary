# 评测资产

- `corpus/cases.json`：`synthetic.v1`，20 条单条分析 + 4 组每日回顾；全部为虚构输入，含独立预期标签。
- `corpus/contract-responses.json`：独立手写合约响应；没有真实模型输出，不从标注自动推导响应。
- `corpus/prompt-registry.json`：提示词版本与哈希登记。
- `baselines/contract-v1.json`：已审核的确定性离线基线，省略运行环境、原始输出与耗时。它不是模型质量基线。
- `scoring.py`：一对一锚点匹配、原始 / 最终结果分开计分、版本化门禁。
- `run.py`：默认离线；真实评测显式开启，只发送合成输入，从指定环境变量读取密钥。
- `compare.py`：同数据集、评分器、门禁、样例集合和模式才可比较；逐例检查退步。
- `corpus/about-cases.json` 与 `about_contract.py`：11 个个人档案的手写合约样例，检查引用、近期范围、推测标记与月度双向来源。不是模型质量分数。

```bash
python -m evals.run --baseline evals/baselines/contract-v1.json
python -m evals.about_contract
```

报告默认保存在被 Git 忽略的 `.eval-results/`。更换提示词、数据或评分规则时保留版本边界；不要刷新基线隐藏退步。真实实验命令、指标定义、计费与人工复核要求见[迭代说明](../docs/iteration.md)。
