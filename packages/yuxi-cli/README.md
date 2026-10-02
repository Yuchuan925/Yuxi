# @xerrors/yuxi

Yuxi Public v1 command-line client. Requires Node.js 22 or newer.

```bash
npm install --global @xerrors/yuxi
yuxi remote add production https://example.com
yuxi remote use production
yuxi login
yuxi agent list
yuxi chat --agent default-chatbot
yuxi kb query --kb-id <id> "your question"
```

Configuration is stored at `~/.yuxi/config.json` with restrictive file permissions. The first release focuses on authentication, Public Agent/Thread APIs, terminal chat, SSE observation, and knowledge-base retrieval. Eval, Langfuse, upload, and administration APIs are intentionally not included.
