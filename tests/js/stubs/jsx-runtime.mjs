// react/jsx-runtime stub: elements are plain { type, props, key } objects.
export const Fragment = Symbol.for('react.fragment')

export function jsx(type, props, key) {
  return { $$typeof: 'element', type, props: props ?? {}, key: key === undefined ? null : key }
}

export const jsxs = jsx
export const jsxDEV = jsx
