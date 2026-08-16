# Phase1 详细开发设计

## 目标与范围

将 Google、iOS、Web 三类价格 Excel 转为统一价格库，并提供检查、确认、查询和版本追溯。汇率、调价、自动修改渠道不包含在本阶段。

## 标准对象

`StandardPrice(channel, country_code, usd_tier, currency, local_price, product_id?, source_sheet, source_row, source_column)`。

`ImportIssue(severity, code, message, location, raw_value, suggestion)`，严重级别为 `ERROR` 或 `WARNING`。

## 工作流

```text
创建任务 → 校验文件 → Adapter解析 → 标准化 → 规则校验
→ 预览统计 → 用户确认 → 事务生成版本 → 原文件归档 → 价格库展示
```

## 渠道规则

- Google：商品 ID 映射/尾部数值识别 USD 档位，列标题提供国家与币种。
- iOS：Sheet 名表示 USD 档位，行数据提供国家、币种和价格。
- Web：积分/商品档对应 USD 档位，国家标题解析国家与币种；忽略收入字段。

## 检查

必须检查文件类型、工作表、必需表头、国家、币种、档位、价格、重复记录、同国家多币种、14 档覆盖率。覆盖率不足可以展示，但是否阻断由渠道规则决定；P1-004 Google 默认将缺少任何已配置档位列视为错误。

## 完成定义

三渠道真实样本通过；错误样本被准确拒绝；活动版本查询正确；历史版本和原文件可追溯；全量自动测试通过。
