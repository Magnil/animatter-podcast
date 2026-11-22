/**
 * 模板包在浏览器侧的三件算术：下载文件名、覆盖提示、包大小。
 *
 * 为什么单独一个文件：这三件事都有「看起来对、实际错」的写法，而且都在面板里，组件测试
 * 在这个仓库里是走查脚本（要跑浏览器），太贵。放在纯函数里就能用 `node --test` 钉住。
 *
 * 最值得钉的是 `filenameFromDisposition`：服务端同时给了 `filename=` 与 RFC 5987 的
 * `filename*=UTF-8''…`，后者才是中文名。只读前者会得到 `template.template`——一个没有
 * 任何信息的文件名，而且不会报错。
 */

/** 包的扩展名。`.template` 而不是 `.zip`：双击不该解压，它只属于这个程序。 */
export const PACKAGE_EXT = ".template";

/**
 * 从 `Content-Disposition` 里取下载名。
 *
 * 三种形状按可信度排序：
 * 1. `filename*=UTF-8''%E6%92%AD%E5%AE%A2.template` —— RFC 5987，唯一能带中文的写法；
 * 2. `filename="播客.template"` —— 老式引号写法，中文常常已经被服务器转义或丢弃；
 * 3. 都没有 —— 用调用方给的兜底名。
 *
 * 顺序不能反：同时存在时 `filename*` 才是服务端真正想给的那个。
 */
export function filenameFromDisposition(header: string | null, fallback = `模板${PACKAGE_EXT}`): string {
  if (!header) return fallback;
  const extended = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(header);
  if (extended?.[1]) {
    try {
      const decoded = decodeURIComponent(extended[1].trim().replace(/^"|"$/g, ""));
      if (decoded) return decoded;
    } catch {
      // 畸形的百分号转义：落到老式写法，不要因为一个坏头就让导出失败。
    }
  }
  const plain = /filename\s*=\s*("([^"]*)"|([^;]+))/i.exec(header);
  const raw = (plain?.[2] ?? plain?.[3] ?? "").trim();
  return raw || fallback;
}

/** 包大小，一句话。模板包通常 1–3 MB，超过 10 MB 说明字体被带进了好几份。 */
export function packageSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "";
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

/**
 * 换模板之前要说的话。
 *
 * 不是「确定吗？」——那是在问一个用户已经决定的事。要说的只有两条**他看不见的后果**：
 * 改过的文字和换过的头像会没（它们存在 `edits_json` 上，属于旧模板），而 PSD 原件还留在
 * 项目目录里，随时可以再导入一次。
 */
export const REPLACE_WARNING = "换模板会替换现在的文字和头像；原来导入的 PSD 不会被删。";
