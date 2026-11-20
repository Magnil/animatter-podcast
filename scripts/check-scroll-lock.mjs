// 走查：点完时间轴之后，字幕区还能不能滚动（第 84 轮）。
//
// 用户报的：「点击时间轴上后，左边的字幕区就被锁在那句话上了，无法再继续滚动。
// 滚动时会自动跳回来。」
//
// 根因是 reveal 效果的依赖项里有一个每次渲染都会新建的对象（`selectionRange()`
// 的返回值），于是滚动引起的重渲染又把它唤醒，发现选中的词不在屏上就滚回去。
//
// 这个脚本做的就是复现那条路径：点时间轴 → 选中一个词 → 连续几次滚动 → 读
// scrollTop 有没有被弹回。修好之前每一步都会回到同一个值，修好之后应当单调地
// 往下走。
//
// 用真实滚轮事件（`Input.dispatchMouseEvent` 的 mouseWheel），不是改 `scrollTop`：
// 直接赋值会绕过 React 的 onScroll，也就绕过了这条 bug 的触发条件。

const state = `(() => {
  const list = document.querySelector(".sentence-list");
  return {
    scrollTop: list ? Math.round(list.scrollTop) : null,
    scrollHeight: list ? Math.round(list.scrollHeight) : null,
    clientHeight: list ? Math.round(list.clientHeight) : null,
    selected: [...document.querySelectorAll(".token.selected")].map((el) => Number(el.dataset.token)),
    firstOrd: (() => {
      const row = document.querySelector(".sentence");
      return row ? Number(row.dataset.ord) : null;
    })(),
  };
})()`;

export default async ({ evaluate, mouse, send, shot, wait, out }) => {
  await wait(2600);

  const canvas = await evaluate(`(() => {
    const r = document.querySelector("canvas").getBoundingClientRect();
    return {
      left: Math.round(r.left),
      top: Math.round(r.top),
      width: Math.round(r.width),
      height: Math.round(r.height),
    };
  })()`);

  const beforeSeek = await evaluate(state);

  // 1. 点时间轴：选中一个词，列表会滚到它那里。
  const seekX = canvas.left + Math.round(canvas.width * 0.62);
  const seekY = canvas.top + Math.round(canvas.height * 0.55);
  await mouse("mousePressed", seekX, seekY);
  await mouse("mouseReleased", seekX, seekY);
  await wait(1000);
  const afterSeek = await evaluate(state);

  // 2. 滚轮往下滚几次，每次都读 scrollTop。
  const list = await evaluate(`(() => {
    const r = document.querySelector(".sentence-list").getBoundingClientRect();
    return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) };
  })()`);

  const wheel = async (deltaY) => {
    await send("Input.dispatchMouseEvent", {
      type: "mouseWheel",
      x: list.x,
      y: list.y,
      deltaX: 0,
      deltaY,
      pointerType: "mouse",
    });
  };

  const steps = [];
  for (let i = 0; i < 4; i += 1) {
    await wheel(240);
    await wait(350);
    steps.push({ step: i, ...(await evaluate(state)) });
  }

  // 3. 再往回滚两次：上滚同样不该被弹回。
  const back = [];
  for (let i = 0; i < 2; i += 1) {
    await wheel(-240);
    await wait(350);
    back.push({ step: i, ...(await evaluate(state)) });
  }

  // 4. 再点一次时间轴（选区变化），确认新的定位仍然生效——修的是"滚动被锁"，
  //    不是把自动定位整个关掉。
  const seekX2 = canvas.left + Math.round(canvas.width * 0.3);
  await mouse("mousePressed", seekX2, seekY);
  await mouse("mouseReleased", seekX2, seekY);
  await wait(1000);
  const afterSecondSeek = await evaluate(state);

  await shot(`${out}`);

  // 「滚动被锁」的判定要看**相邻两步之间**有没有动，而不是「和某个基准值不同」。
  // 被锁住时的表现是：第一次滚轮把 scrollTop 推到一个新值，然后每一次都被弹回
  // **同一个**值——四步全都不同于 afterSeek，却一步都没真正走动。第一版就是这么
  // 写的，于是在没修的版本上判成了「正常」。
  const walk = [afterSeek, ...steps].map((step) => step.scrollTop);
  const advanced = walk.slice(1).filter((value, i) => value !== walk[i]);
  const result = {
    beforeSeek,
    afterSeek,
    steps,
    back,
    afterSecondSeek,
    checks: {
      // 点时间轴确实选中了词
      seekSelectedWord: afterSeek.selected.length === 1,
      // 点时间轴确实滚到了那里（否则这条 bug 根本没被触发）
      seekScrolled: afterSeek.scrollTop !== beforeSeek.scrollTop,
      // 下滚四步每步都换了位置：没有被弹回去
      scrollNotLocked: advanced.length === 4,
      // 回滚也能生效
      scrollsBack: back.some((step) => step.scrollTop < steps[steps.length - 1].scrollTop),
      // 新的定位仍然有效
      secondSeekMoved: afterSecondSeek.scrollTop !== back[back.length - 1].scrollTop,
      secondSeekSelected: afterSecondSeek.selected.length === 1,
    },
  };
  const { writeFileSync } = await import("node:fs");
  writeFileSync(`${out}.json`, JSON.stringify(result, null, 2), "utf8");
  return result;
};
