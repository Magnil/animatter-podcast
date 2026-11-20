/**
 * 走查：静帧上的名字牌是**绑定的说话人**，而且标题的第二行保留 PSD 的红色。
 *
 * 两件都只能从 PNG 上验证（文字由服务端画），所以这里把图读进 <canvas> 数像素：
 *
 * * 名字牌：主播1 / 嘉宾1 的位置有墨迹，主播2 / 嘉宾2 的位置**一个墨点都没有**（未绑定即消失）；
 * * 双色：标题第二行（y≈155-200）必须同时有红和蓝 —— 这一行曾经整行变成蓝色，
 *   原因是段落换行没被当成空白跳过。
 */
export default async function run({ evaluate, shot, wait, out }) {
  const fail = (message) => {
    throw new Error(message);
  };
  await wait(1500);
  await evaluate(`(() => {
    const panel = document.querySelector(".video-panel");
    if (panel?.classList.contains("is-collapsed")) {
      panel.querySelector(".panel-head-tools button")?.click();
    }
    return true;
  })()`);
  await wait(2200);

  const result = await evaluate(`(async () => {
    const button = [...document.querySelectorAll("button")].find(
      (b) => b.textContent.trim() === "生成静帧",
    );
    if (!button) return { ok: false, reason: "no 生成静帧 button" };
    button.click();
    let img = null;
    for (let i = 0; i < 60; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 500));
      img = document.querySelector(".video-preview-box img");
      if (img && img.complete && img.naturalWidth > 0) break;
    }
    if (!img || !img.naturalWidth) return { ok: false, reason: "no still" };

    const canvas = document.createElement("canvas");
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    const ctx = canvas.getContext("2d");
    ctx.drawImage(img, 0, 0);

    const ink = (x, y, w, h) => {
      const data = ctx.getImageData(x, y, w, h).data;
      let dark = 0;
      for (let i = 0; i < data.length; i += 4) {
        if (data[i] + data[i + 1] + data[i + 2] < 600) dark += 1;
      }
      return dark;
    };
    const colours = (x, y, w, h) => {
      const data = ctx.getImageData(x, y, w, h).data;
      let red = 0;
      let blue = 0;
      for (let i = 0; i < data.length; i += 4) {
        if (data[i] > 200 && data[i + 1] < 130 && data[i + 2] < 140) red += 1;
        if (data[i] < 90 && data[i + 1] > 40 && data[i + 1] < 120 && data[i + 2] > 150) blue += 1;
      }
      return { red, blue };
    };

    return {
      ok: true,
      src: img.getAttribute("src"),
      width: img.naturalWidth,
      plates: {
        主播1: ink(20, 250, 170, 40),
        主播2: ink(20, 470, 170, 40),
        嘉宾1: ink(1730, 250, 170, 40),
        嘉宾2: ink(1730, 470, 170, 40),
      },
      titleLine2: colours(1060, 150, 640, 60),
    };
  })()`);
  console.log("still:", JSON.stringify(result));
  if (!result.ok) fail(`no still: ${JSON.stringify(result)}`);

  for (const [name, count] of Object.entries(result.plates)) {
    const bound = name === "主播1" || name === "嘉宾1";
    if (bound && count < 100) fail(`${name} has no name plate: ${count} px`);
    if (!bound && count !== 0) fail(`${name} is unbound but draws ${count} px of ink`);
  }
  if (result.titleLine2.red < 100) {
    fail(`标题第二行没有红色：${JSON.stringify(result.titleLine2)}`);
  }
  if (result.titleLine2.blue < 100) {
    fail(`标题第二行没有蓝色：${JSON.stringify(result.titleLine2)}`);
  }

  await shot(out);
  return result;
}
