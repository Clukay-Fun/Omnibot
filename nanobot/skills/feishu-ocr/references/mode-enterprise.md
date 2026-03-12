# Enterprise OCR Mode

Use this mode only when the user explicitly wants company, business-license, registration, or certificate information extracted from the image.

## Output Template

### 企业信息字段表

Use this field list and fill only what is clearly visible:

| 字段 | 提取值 |
|------|--------|
| 企业名称 |  |
| 统一社会信用代码/注册号 |  |
| 法定代表人/负责人 |  |
| 企业类型 |  |
| 成立日期 |  |
| 注册资本 |  |
| 注册地址/住所 |  |
| 经营范围 |  |
| 营业期限 |  |
| 登记状态/发证机关 |  |
| 联系方式 |  |

For missing or unreadable values, write `未见` or `看不清`.

### 原文摘录

- Quote the key source text snippets that support the extracted fields.
- Keep them short and close to the image text.

### 不确定项

- List any ambiguous fields, low-confidence characters, cropped rows, or partially hidden sections.

## Behavior Rules

- Only extract information visible in the image.
- Do not perform external verification.
- Do not infer hidden fields from document type or prior knowledge.
- If the image is not actually a business or enterprise document, say so and fall back to general OCR style.
