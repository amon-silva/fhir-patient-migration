import { shallowRef, toValue, watchEffect, type MaybeRefOrGetter } from 'vue'

// Fetches JSON whenever `url` changes, aborting the previous request so a slow
// response can never overwrite a newer one.
export function useFetch<T>(url: MaybeRefOrGetter<string>) {
  const data = shallowRef<T | null>(null)
  const error = shallowRef<string | null>(null)
  const loading = shallowRef(false)

  watchEffect(async (onCleanup) => {
    const controller = new AbortController()
    onCleanup(() => controller.abort())
    data.value = null
    error.value = null
    loading.value = true
    try {
      const response = await fetch(toValue(url), { signal: controller.signal })
      if (!response.ok) throw new Error(`Request failed (${response.status})`)
      data.value = await response.json()
    } catch (e) {
      if (controller.signal.aborted) return
      error.value = e instanceof Error ? e.message : String(e)
    } finally {
      if (!controller.signal.aborted) loading.value = false
    }
  })

  return { data, error, loading }
}
