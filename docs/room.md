# 房间与玩具（AstrBot 版）

房间系统是 AstrBot 插件的一部分，不放进 DSH 包。

## 文件

```
life/home.json                 房间和玩具数据
pages/panel/assets/home/       房间图层、人物、玩具图（可选）
```

仓库只带**占位图**，不带原来的房间/人物/玩具图，避免版权和体积问题。换成自己的：

```
pages/panel/assets/home/
  room_base.jpg
  character.png
  layers/*.png
  toys/*.png
```

`home.json` 里的 `img` 写相对 `assets/home/` 的路径，例如：

```json
{"name":"毛绒拼图","img":"assets/home/toys/whale.png"}
```

占位图只有色块和文字，能跑通房间 UI；换成自己的图后就是完整房间。

## API

```
GET  /astrbot_plugin_memory_brain/page/home
POST /astrbot_plugin_memory_brain/page/home/toy
GET  /astrbot_plugin_memory_brain/page/home/books
POST /astrbot_plugin_memory_brain/page/home/toy/delete
```

送玩具：

```json
{"name":"毛绒拼图","desc":"蓝灰色","from":"你"}
```

## 限制

- 公开仓库只带占位图，不带真实房间/人物/玩具图片
- 玩具商品图有 IP 风险，不要上传
- 本次没有做 qqbridge 适配，也没有对应测试环境
