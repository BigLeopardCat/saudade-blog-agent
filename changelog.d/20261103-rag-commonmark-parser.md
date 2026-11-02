- RAG Markdown 边界改用 markdown-it-py 的 CommonMark token/source map，支持 ATX/Setext 标题，
  正确排除代码、HTML 与容器内局部标题；分节及后代范围直接切原文并保留 CRLF、Unicode。
  读取视图版本与分块空间纳入 parser 策略，旧小节/续读版本明确过期，候选使用同源读取版本。
