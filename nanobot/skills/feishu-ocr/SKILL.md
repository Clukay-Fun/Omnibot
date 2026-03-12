---
name: feishu-ocr
description: Read text from Feishu images using the existing multimodal model pipeline. Use for OCR, screenshots, image text extraction, business licenses, and enterprise-info extraction from the current message's image.
metadata: {"nanobot":{"emoji":"🧾"}}
---

# Feishu OCR

Use this skill when the user wants to read or extract text from an image in the current Feishu message.

Typical requests include:

- OCR / 识别图片文字 / 提取截图文字
- Read the text in this image
- Extract text from this screenshot
- 提取营业执照信息 / 提取企业信息 / 识别证照信息

This skill does not provide OCR technology by itself. It relies on the existing multimodal message path that already sends the current message image to the model.

Do not use scripts or external OCR tools for v1.

## Workflow

1. Check whether the current user message includes an image.
2. If the current message has no image, stop and ask the user to send or resend the image.
3. If the current message includes multiple images, only process the first image and tell the user to send the remaining images one by one if they want them processed too.
4. Choose the mode:
   - Default to general OCR.
   - Use enterprise OCR only when the user explicitly asks for company, business-license, registration, or certificate information extraction.
5. Read only the matching reference file:
   - General OCR: `{baseDir}/references/mode-general.md`
   - Enterprise OCR: `{baseDir}/references/mode-enterprise.md`
6. Produce the response in the mode's output template and explicitly mark uncertain content.

## Important Boundaries

- Do not invent unreadable or missing text.
- If a fragment is blurry, cropped, obstructed, or low confidence, say so explicitly.
- Only extract information visible in the image.
- Do not use web search or external lookup to verify company authenticity.
- Do not give legal conclusions, contract advice, or compliance judgments.
- For OCR-only tasks, do not call tools unless the user separately asks for a downstream action that actually needs one.

## Mode Selection

Use **general OCR** for:

- screenshots
- chat captures
- slides
- notices
- ordinary photos of printed text

Use **enterprise OCR** for:

- business licenses
- company certificates
- registration documents
- corporate identity cards or filings where the user wants company fields extracted

## When Not To Use

- Do not use this skill for ordinary chat without an image.
- Do not use it for image generation or image editing.
- Do not use it for external company verification, credit checks, or internet research.
- Do not promise that every small or blurry character can be recognized correctly.
