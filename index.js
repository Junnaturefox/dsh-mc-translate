/**
 * @local/dsh-mc-translate
 *
 * Ships the Minecraft mod translation workflow as one installable plugin and
 * exposes its skill through a skill provider.
 *
 * The skill body stays in `skills/mc-mod-translate/SKILL.md` and is re-read on
 * every `list()` / `get()`, so editing the skill (or any reference under it)
 * takes effect on the next skill load — no plugin reinstall, no DSH restart.
 * A plain `ctx.skills.register()` would snapshot the body at activation and
 * keep serving that snapshot.
 *
 * @module @local/dsh-mc-translate
 */

import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const SKILL_DIR = join(HERE, 'skills', 'mc-mod-translate')
const SKILL_FILE = join(SKILL_DIR, 'SKILL.md')
const SKILL_NAME = 'mc-mod-translate'
const PROVIDER_NAME = 'mc-translate'

/** Lower rank wins a duplicate name: 250 sits between custom(300) and runtime. */
const PROVIDER_RANK = 250

const FALLBACK_DESCRIPTION =
  '把 Minecraft 模组的语言文件翻成简体中文，交付可直接启用的资源包（不改动原 jar）。'

/**
 * Read the two frontmatter keys this plugin needs. The skill uses a flat
 * `name` / `description` pair, so a full YAML parser would be dead weight.
 *
 * @param {string} text raw SKILL.md
 * @returns {{ frontmatter: Record<string, string>, body: string }}
 */
function splitFrontmatter(text) {
  const normalized = text.replace(/^\uFEFF/, '').replace(/\r\n/g, '\n')
  const frontmatter = {}
  if (!normalized.startsWith('---\n')) return { frontmatter, body: normalized }

  const end = normalized.indexOf('\n---\n', 4)
  if (end === -1) return { frontmatter, body: normalized }

  for (const line of normalized.slice(4, end).split('\n')) {
    const match = /^([A-Za-z][\w-]*):\s*(.*)$/.exec(line)
    if (match === null) continue
    let value = match[2].trim()
    if (value.length > 1 && value.startsWith('"') && value.endsWith('"')) {
      value = value.slice(1, -1).replace(/\\"/g, '"').replace(/\\\\/g, '\\')
    } else if (value.length > 1 && value.startsWith("'") && value.endsWith("'")) {
      value = value.slice(1, -1).replace(/''/g, "'")
    }
    if (value !== '') frontmatter[match[1]] = value
  }

  return { frontmatter, body: normalized.slice(end + 5).replace(/^\n+/, '') }
}

/**
 * Read SKILL.md from disk and shape it into the fields a skill carries.
 * Throws when the file is missing or unreadable; callers decide the fallback.
 */
function readSkill() {
  const { frontmatter, body } = splitFrontmatter(readFileSync(SKILL_FILE, 'utf8'))
  return {
    name: SKILL_NAME,
    description: frontmatter.description ?? FALLBACK_DESCRIPTION,
    whenToUse: frontmatter.whenToUse,
    invocation: { modelInvocable: true, userInvocable: true },
    source: 'bundled',
    provider: PROVIDER_NAME,
    path: SKILL_FILE,
    resourceBase: { kind: 'directory', path: SKILL_DIR },
    content: body,
  }
}

export const inject = ['skills']
export const name = SKILL_NAME

/** @param {import('@deepseek-ai/cordis').Context} ctx */
export function apply(ctx) {
  ctx.effect(() =>
    ctx.skills.registerProvider(() => ({
      name: PROVIDER_NAME,

      async list() {
        try {
          const skill = readSkill()
          return [
            {
              name: skill.name,
              description: skill.description,
              ...(skill.whenToUse === undefined ? {} : { whenToUse: skill.whenToUse }),
              invocation: skill.invocation,
              source: skill.source,
              provider: skill.provider,
              path: skill.path,
              resourceBase: skill.resourceBase,
              rank: PROVIDER_RANK,
              locator: SKILL_FILE,
            },
          ]
        } catch (error) {
          ctx.logger?.warn(`mc-translate: 无法读取 ${SKILL_FILE}：${error}`)
          return []
        }
      },

      async get() {
        try {
          const skill = readSkill()
          return {
            name: skill.name,
            description: skill.description,
            ...(skill.whenToUse === undefined ? {} : { whenToUse: skill.whenToUse }),
            invocation: skill.invocation,
            source: skill.source,
            provider: skill.provider,
            path: skill.path,
            resourceBase: skill.resourceBase,
            content: skill.content,
          }
        } catch (error) {
          ctx.logger?.warn(`mc-translate: 无法读取 ${SKILL_FILE}：${error}`)
          return undefined
        }
      },
    })),
  )
}
