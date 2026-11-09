#!/usr/bin/env node
// read_content — GET /content
//
// 服务端行为：不带 offset 时返回**整篇全文**并忽略 limit。SDK / MCP 都会
// 自动补 offset=0、limit=4096；这里是直接打 HTTP，所以必须自己补，
// 否则一次调用可能把几十万码点塞进模型上下文。
import { callSciverse, readJsonArg } from "./_common.mjs";

const DEFAULT_OFFSET = 0;
const DEFAULT_LIMIT = 4096;

const args = readJsonArg();
if (!args.doc_id) {
  console.error("[read_content] 必须提供 doc_id 字段。");
  process.exit(2);
}

const query = {
  doc_id: args.doc_id,
  offset: args.offset ?? DEFAULT_OFFSET,
  limit: args.limit ?? DEFAULT_LIMIT,
};

const result = await callSciverse("GET", "/content", { query });
console.log(JSON.stringify(result));
