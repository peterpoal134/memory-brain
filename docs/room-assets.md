# 房间素材准备清单

仓库里的房间素材是**占位图**。  
想让面板和照片变成你自己的房间，需要自己准备下面的图片。

> 不要直接使用有版权的商品图、动漫 IP 形象或别人的照片。  
> 只用自己拍摄、自己绘制或明确允许使用的素材。

## 一、必需文件

目录：

```text
plugins/astrbot_plugin_memory_brain/pages/panel/assets/home/
```

| 文件 | 用途 | 建议规格 |
|---|---|---|
| `room_base.jpg` | 生图时的人物所在房间 | 1600×900，JPG |
| `character.png` | 面板里的全身人物 | 约 790×1000，PNG，透明背景 |
| `bg.png` | 房间面板背景 | 1600×900 |
| `curtain_closed_src.png` | 关窗帘状态 | 600×1000，PNG，透明 |
| `curtain_open_src.png` | 开窗帘状态 | 600×1000，PNG，透明 |

没有这些文件，面板会缺图，生图/视频也会失败。

## 二、房间分层图

目录：

```text
pages/panel/assets/home/layers/
```

需要这些透明 PNG：

| 文件 | 家具 | 参考尺寸 |
|---|---|---|
| `bed.png` | 床 | 500×260 |
| `bookshelf.png` | 书架 | 300×500 |
| `chair.png` | 椅子 | 260×300 |
| `desk.png` | 书桌 | 500×220 |
| `lamp.png` | 台灯 | 180×220 |
| `plant.png` | 绿植 | 260×350 |
| `rug.png` | 地毯 | 700×220 |
| `toy_shelf.png` | 玩具架 | 260×400 |
| `window.png` | 窗户 | 400×400 |
| `ceiling_light.png` | 吸顶灯 | 300×120 |

图层是叠加在 `bg.png` 上的，不需要把家具画进背景。  
每张图保持同一视角、同一比例，留透明边距。

## 三、可选人物参考图

| 文件 | 用途 |
|---|---|
| `character_photo_ref.png` | 写实版生图参考，建议正面半身/全身 |
| `character_video_ref.png` | 生视频首帧参考；没有就退回 `character.png` |

这两张**只在作者本机用于生成**，公开仓库没有。  
网友需要自己准备，且必须是自己的角色或已授权形象。

## 四、玩具、衣服、配饰图

目录：

```text
pages/panel/assets/home/toys/
pages/panel/assets/home/clothes/
pages/panel/assets/home/accessories/
```

规则：

```text
toys/<toy_id>.png
clothes/<cloth_id>.png
accessories/<acc_id>.png
```

要求：

- PNG，透明背景
- 单个物体，不要带手、背景、文字水印
- 文件名和 `home.json` 里的 `id` 一致
- 衣服建议平铺图；配饰建议正面图

## 五、home.json

路径：

```text
life/home.json
```

如果不存在，插件会用一份默认示例。  
要加入自己的东西，按下面结构写：

```json
{
  "room": "我的房间",
  "theme": "night",
  "toys": [
    {
      "id": "toy_puzzle",
      "name": "五百片拼图",
      "type": "puzzle",
      "desc": "海边小镇，拼到一半总被猫踩乱",
      "novelty": 0.65,
      "from": "主人",
      "rarity": "喜欢",
      "durability": 0.5,
      "liking": 0.75
    }
  ],
  "clothes": [
    {
      "id": "cloth_blue_dress",
      "name": "蓝裙子",
      "desc": "蓝色连衣裙",
      "from": "主人"
    }
  ],
  "accessories": [
    {
      "id": "acc_glasses",
      "name": "圆框眼镜",
      "desc": "细边圆框",
      "from": "主人"
    }
  ],
  "wearing": "",
  "wearing_accessories": []
}
```

图片可以放在对应目录，让文件名等于 `id.png`；  
也可以从面板上传，插件会自动转成 PNG。

## 六、最小可用版本

如果只想先跑起来，至少准备：

```text
room_base.jpg
character.png
bg.png
curtain_closed_src.png
curtain_open_src.png
layers/ 下 10 张家具图
```

其余玩具、衣服、配饰可以之后慢慢加。
