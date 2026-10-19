# 参与开发

Python 3.10+ 即可运行。执行 `python -m unittest discover -s tests -v` 运行检查，`node --check web/app.js` 检查脚本语法。

使用 `--data-dir` 指向独立目录，避免用个人日记验证导入、删除或恢复。`tests/fake_provider.py` 是明确标注的本地接口替身，不是真实模型。

涉及 AI 的修改应分别验证：请求参数兼容性；状态、引用和持久化等确定性机制；有标注样本上的语义质量。运行 `python -m evals.run --baseline evals/baselines/contract-v1.json` 检查离线合约；它使用手写响应，不证明模型质量。真实模型评测、版本登记与发布门禁见[迭代说明](docs/iteration.md)。

先加入能复现问题的合成样例，再修改实现。提示词、数据集、评分器与门禁分别管理版本；不通过随意更新基线隐藏退步。新增功能请同步验收指标和[路线图](ROADMAP.md)。

提交前执行 `python tools/audit_repository.py`，检查 Git 跟踪内容中没有本机数据、凭证与私人迁移记录。该检查不能替代人工审阅。

提交问题请使用合成文本与最小复现，不上传真实日记、密钥、个人备份或完整日志。产品范围变化需要同步更新设计、数据生命周期与验收依据。
