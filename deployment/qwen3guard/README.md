# 本机 Qwen3Guard CPU 服务

这个部署直接运行 llama.cpp 的 `llama-server`，不经过 OneJev API。复用本机已经验证的 `onejev-local:cpu` 中 b11561 CPU 推理程序；该镜像作为构建来源保留，但不会运行 OneJev Python 服务。Docker Desktop 启动后容器自动恢复；手动停止的容器不自动恢复。

将本目录复制到固定位置，把模型放在 `models/Qwen3Guard-Gen-4B.Q4_K_M.gguf`，然后运行：

```powershell
docker --context desktop-linux compose up -d --build
docker --context desktop-linux compose ps
```

模型来源：`mradermacher/Qwen3Guard-Gen-4B-GGUF`，revision `b3b010b37c75197c5e887fa7dce9ff907d0be6ff`。文件大小 2,716,068,928 字节，SHA256：

```
c81d88da8e1f90a80d1b892a29fb3deac592fc5f321b35fc964a568398c1dbfb
```

只监听本机 `http://127.0.0.1:8001`；健康检查是 `GET /health`。原生接口是 `POST /completion`，插件内置官方单条用户消息审核模板。插件配置：

```json
{
  "backend": "qwen3guard",
  "base_url": "http://127.0.0.1:8001",
  "model": "Qwen3Guard-Gen-4B-Q4_K_M",
  "qwen": {
    "categories": ["敏感话题", "暴力内容", "不当行为", "违法行为", "色情"],
    "include_controversial": true,
    "act_on_controversial": false
  }
}
```

固定 4 CPU、单推理槽、16K 上下文、不进行输入滑动截断，内存最多 8GiB。16K 是服务容量，超长消息仍记为未完整审核。Docker 日志每份最多 10MB，保留 3 份。模型结果、本地证据和统计另由插件按普通 14 天、可疑 60 天保留。更新模型时需同时修改模型别名以隔离旧缓存。

```powershell
docker --context desktop-linux compose stop
docker --context desktop-linux compose restart
docker --context desktop-linux compose logs --tail 50
```

模型及官方模板源于 [Qwen3Guard-Gen-4B](https://huggingface.co/Qwen/Qwen3Guard-Gen-4B)，Apache-2.0。插件冻结的模板版本是 `6ec42827da0c1ff11e7a49dc269d2e810d27e108`。这里只保留运行文件，模型大文件不要提交 Git。
