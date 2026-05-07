# Risk Notice / 风险提示

## English

This project is an MVP for demo-first engineering validation. It is not financial,
investment, tax, or legal advice.

- Crypto assets are highly volatile. Prices can move sharply, gap, or become illiquid.
- Fees, slippage, spreads, and funding rates can turn a seemingly good signal into a loss.
- OKX APIs, network connections, local storage, and exchange services can fail or return
  delayed, partial, or unexpected data.
- AI-generated explanations are not predictions. They summarize signal and risk context;
  they do not know the future.
- Use demo trading first. Do not connect this MVP to real capital until the full system has
  been audited, monitored, and tested under realistic conditions.
- Do not use API keys with withdrawal permission. Trading keys should be scoped narrowly.
- Enable an IP whitelist for every exchange API key.
- Never commit secrets: no `.env`, API keys, passphrases, account exports, logs with
  credentials, or private trading data.
- Live mode is intentionally blocked until OKX contract-size conversion is implemented.
  The current demo sizing uses USDT notional as a placeholder and is not suitable for live
  order sizing.

## 中文

本项目是模拟盘优先的工程验证 MVP，不构成金融、投资、税务或法律建议。

- 加密资产波动极高，价格可能剧烈波动、跳空，或在极端情况下流动性不足。
- 手续费、滑点、买卖价差和资金费率都可能让看似合理的信号变成亏损。
- OKX API、网络连接、本地存储和交易所服务都可能失败，或返回延迟、不完整、异常数据。
- AI 解释不是预测。它只是总结信号和风控上下文，并不能预知未来行情。
- 必须先使用模拟交易。未经完整审计、监控和真实场景压力测试，不要接入真实资金。
- 不要使用带提现权限的 API Key。交易 Key 应尽可能最小权限。
- 每个交易所 API Key 都应启用 IP 白名单。
- 永远不要提交密钥：包括 `.env`、API Key、passphrase、账户导出、含凭证的日志或私有交易数据。
- 实盘模式在实现 OKX 合约张数换算前会被主动阻断。当前 demo 仓位使用 USDT 名义金额作为占位，
  不适合真实下单 sizing。
