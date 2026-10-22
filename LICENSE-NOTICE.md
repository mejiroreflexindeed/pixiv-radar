# 第三方代码来源说明

## dong-jpg/pixiv-novel-sync（MIT License）

`backend/app/storage/connection.py` 中的 SQLite 连接设计（WAL 日志模式、
`busy_timeout`、显式外键、`BEGIN IMMEDIATE` 短事务）参考了
[dong-jpg/pixiv-novel-sync](https://github.com/dong-jpg/pixiv-novel-sync)
的 `src/pixiv_novel_sync/storage/connection.py`（提交 `caace7c2`）。

除上述连接层设计外，本项目未复制该仓库的其他源码。

来源仓库 `LICENSE` 文件原文（MIT License）：

```text
MIT License

Copyright (c) 2026 dong-jpg

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
