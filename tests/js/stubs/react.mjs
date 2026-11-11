// react stub: hooks are implemented by the shallow renderer (../render.mjs).
import {
  Fragment,
  createContext,
  startTransition,
  useCallback,
  useContext,
  useDebugValue,
  useDeferredValue,
  useEffect,
  useId,
  useImperativeHandle,
  useInsertionEffect,
  useLayoutEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
  useSyncExternalStore,
  useTransition
} from '../render.mjs'

export {
  Fragment,
  createContext,
  startTransition,
  useCallback,
  useContext,
  useDebugValue,
  useDeferredValue,
  useEffect,
  useId,
  useImperativeHandle,
  useInsertionEffect,
  useLayoutEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
  useSyncExternalStore,
  useTransition
}

export function createElement(type, props, ...children) {
  const { key = null, ...rest } = props ?? {}
  const merged = { ...rest }

  if (children.length === 1) {
    merged.children = children[0]
  } else if (children.length > 1) {
    merged.children = children
  }

  return { $$typeof: 'element', type, props: merged, key }
}

export function isValidElement(value) {
  return value !== null && typeof value === 'object' && value.$$typeof === 'element'
}

export function cloneElement(element, props, ...children) {
  const next = createElement(element.type, { ...element.props, ...props, key: props?.key ?? element.key }, ...children)

  return next
}

// Identity-ish wrappers: the renderer calls the underlying function directly.
export function memo(component) {
  return component
}

export function forwardRef(render) {
  const wrapped = props => render(props, props?.ref ?? null)

  wrapped.displayName = render.displayName || render.name || 'ForwardRef'

  return wrapped
}

export function lazy() {
  throw new Error('React.lazy is not supported by the test renderer')
}

export const Children = {
  toArray(children) {
    const out = []
    const visit = child => {
      if (Array.isArray(child)) {
        child.forEach(visit)
      } else if (child !== null && child !== undefined && typeof child !== 'boolean') {
        out.push(child)
      }
    }

    visit(children)

    return out
  },
  count(children) {
    return Children.toArray(children).length
  },
  map(children, fn) {
    return Children.toArray(children).map(fn)
  },
  forEach(children, fn) {
    Children.toArray(children).forEach(fn)
  },
  only(children) {
    return Children.toArray(children)[0]
  }
}

export const StrictMode = ({ children }) => children
export const Suspense = ({ children }) => children

export default {
  Fragment,
  Children,
  StrictMode,
  Suspense,
  cloneElement,
  createContext,
  createElement,
  forwardRef,
  isValidElement,
  lazy,
  memo,
  startTransition,
  useCallback,
  useContext,
  useDebugValue,
  useDeferredValue,
  useEffect,
  useId,
  useImperativeHandle,
  useInsertionEffect,
  useLayoutEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
  useSyncExternalStore,
  useTransition
}
