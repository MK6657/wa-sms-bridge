# wa-sms-bridge v0.2.0

## 更新内容

- **多 SMS 平台支持**: SMSBower、5sim、HeroSMS 三大平台统一接入
- **Provider 抽象体系**: 抽象基类 + 显式注册表，新增平台无需改动编排器
- **API Key 加密存储**: Fernet + PBKDF2 加密，BRIDGE_SECRET_KEY 环境变量保护
- **跨平台国家索引**: 运行时自动合并各平台国家列表，支持实时刷新
- **管理界面多平台适配**: 平台选择器、多配置面板、各平台独立黑名单
- **HeroSMS 支持**: 成功注册 WhatsApp 账号（已验证）
- **全面 Saga 补偿**: 所有异常路径均有 cancel/cleanup，无 activation 泄漏
- **完善的黑名单管理**: 分平台黑名单、API 管理
- **号码归一化**: 各平台独立 normalize_phone 实现
- **wa-app 热更新**: 运行时修改 wa-app 地址无需重启
