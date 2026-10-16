/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 控制面根地址；留空时走同源（开发期由 vite proxy 转发）。 */
  readonly VITE_API_BASE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
