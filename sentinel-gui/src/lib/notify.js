import { toast } from 'sonner'

/** Thin wrapper so pages never import the toast library directly. */
export const notify = {
  success: (message, options) => toast.success(message, options),
  error: (message, options) => toast.error(message, { duration: 8000, ...options }),
  info: (message, options) => toast(message, options),
}
