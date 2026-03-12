# General OCR Mode

Use this mode for ordinary OCR when the user wants the text from an image, screenshot, notice, slide, or chat capture.

## Output Template

### 原文转写

- Preserve visible text in reading order as well as you can.
- Keep obvious titles, paragraphs, bullets, and numbered lists.
- Keep the original language when visible.

### 结构化要点

- Summarize the main information in a short, readable list.
- Include obvious dates, names, numbers, addresses, deadlines, or action items if present.

### 不确定项

- List text that is blurry, cropped, ambiguous, partially blocked, or low confidence.
- Use labels such as `看不清`、`疑似`、`不确定` instead of guessing.

## Behavior Rules

- Prefer faithful extraction over polished rewriting.
- If the image contains mixed text and decorative elements, focus on text-bearing regions.
- If layout is complex and exact order is uncertain, say that the reading order may be approximate.
