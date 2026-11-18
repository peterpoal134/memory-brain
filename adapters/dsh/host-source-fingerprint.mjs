/** Syntax-level matching of a reviewed build. This never executes host source. */
import crypto from 'node:crypto';
import {parse} from './vendor/acorn.mjs';

const maxSourceBytes = 512 * 1024;

export function syntaxFingerprint(source) {
  if (typeof source !== 'string' || Buffer.byteLength(source, 'utf8') > maxSourceBytes) return null;
  try {
    const syntax = parse(source, {ecmaVersion: 'latest', sourceType: 'module', allowHashBang: false});
    // Keep literal representations, directives and syntax structure, including
    // the effects of automatic semicolon insertion. Drop only source offsets.
    const content = JSON.stringify(syntax, (key, value) =>
      key === 'start' || key === 'end' ? undefined : typeof value === 'bigint' ? value.toString() : value);
    return crypto.createHash('sha256').update(content).digest('hex');
  } catch {
    return null;
  }
}

export function methodSyntaxFingerprint(source) {
  return syntaxFingerprint('class VerifiedHostMethod {\n' + source + '\n}');
}
