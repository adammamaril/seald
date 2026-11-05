<div align="center">
  <img src="assets/seald.svg" width="160" alt="A little seal holding an envelope">
  <h1>seald</h1>
  <p><strong>One command for every credential, for operators and agents.</strong><br>
  Hidden prompt in → <a href="https://github.com/getsops/sops">SOPS</a> + <a href="https://github.com/FiloSottile/age">age</a> ledger → env vars out. One Bash script, no daemon.</p>
  <p>
    <a href="https://github.com/adammamaril/seald/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/adammamaril/seald/ci.yml?branch=main&style=flat-square&color=22c55e&label=tests" alt="CI"></a>
    <img src="https://img.shields.io/badge/bash-shellcheck%20clean-27272a?style=flat-square" alt="Bash, shellcheck clean">
    <a href="https://github.com/getsops/sops"><img src="https://img.shields.io/badge/sops%20%2B%20age-27272a?style=flat-square" alt="SOPS + age"></a>
    <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-27272a?style=flat-square" alt="MIT license"></a>
  </p>
</div>

## Requirements

Needs only [sops](https://github.com/getsops/sops), plus [age](https://github.com/FiloSottile/age) to make your key:

```sh
brew install sops age               # macOS
sudo pacman -S sops age             # Arch
sudo apt install age                # Debian / Ubuntu   (Fedora: sudo dnf install age)
# no sops package on your distro? use the release binary (arm64: swap amd64):
curl -fsSLo sops https://github.com/getsops/sops/releases/download/v3.13.3/sops-v3.13.3.linux.amd64
sudo install -m 0755 sops /usr/local/bin/sops && rm sops
```

No age key yet? Run once:

```sh
mkdir -p ~/.config/sops/age
age-keygen -o ~/.config/sops/age/keys.txt                                   # private
age-keygen -y ~/.config/sops/age/keys.txt > ~/.config/sops/age/pubs.txt     # public: seal encrypts to these
```

## Quickstart

```sh
git clone https://github.com/adammamaril/seald && cd seald
./seal set GITHUB_TOKEN          # hidden prompt, asked twice
./seal exec -- gh repo list      # gh sees $GITHUB_TOKEN; your shell never does
```

## Usage

| Command | Does |
|---|---|
| `seal set KEY` | Store a secret from a hidden prompt (or stdin). Setting again rotates it. |
| `seal get KEY` | Print it to stdout. |
| `seal list` | Key names only. |
| `seal rm KEY` | Delete it. |
| `seal exec -- CMD…` | Run CMD with every key as an env var. |
| `seal file [-r age1…] [-R FILE] [-f] IN.yaml OUT.enc.yaml` | Encrypt a whole YAML file to recipients (no ledger). |

Ledger: `~/.local/share/seal/secrets.enc.yaml` (`-l` / `SEAL_LEDGER`). Recipients: `~/.config/sops/age/pubs.txt` (`SEAL_RECIPIENTS` / `SEAL_RECIPIENTS_FILE`). Keys must be valid env var names.

## How it works

```
 seal set GITHUB_TOKEN   (you type the value; plaintext)
      │
      ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ seal set                                     umask 077 · refuses bash -x │
│                                                                          │
│ 1. key name      [A-Za-z_][A-Za-z0-9_]*  (becomes an env var name)       │
│ 2. hidden prompt read -s, asked twice, must match and not be empty       │
│                  (or piped / --stdin: the whole of stdin)                │
│ 3. first set     recipients: SEAL_RECIPIENTS or ~/.config/sops/age/pubs  │
│                  creates an empty ledger: temp file, 0600, mv into place │
│ 4. store         json_str (pure bash) | sops set --value-stdin LEDGER    │
│                  then chmod 0600                                         │
│                                                                          │
│ the value is never in argv, never on disk in clear, never echoed         │
└─────────────────────────────────────┬────────────────────────────────────┘
                                      ▼
            ~/.local/share/seal/secrets.enc.yaml (ciphertext, 0600)
```

- **Secrets never touch argv, disk in clear, or your screen.** Values move through pipes into `sops set --value-stdin`; files are 0600; only `get` prints a value; `seal` refuses to run under `bash -x`.
- **Tested:** a unittest suite, including a pseudo-terminal check that the prompt never echoes.

## Keep in mind

- **Agents: use `seal exec`, not `seal get`.** `get` puts plaintext on stdout, and so into agent context and logs.
- **Back up `keys.txt`.** Lose it and you lose the ledger.
- **Pipe with `printf %s`, not `echo`.** Piped input is stored exactly, trailing newline included.

## Development

```sh
python3 -m unittest discover -s tests && shellcheck seal     # install: cp seal ~/.local/bin/
```

## License

MIT © 2026 Genga Labs. See [LICENSE](LICENSE).

<p align="center"><sub>Built and maintained by <strong>Genga Labs</strong>.</sub></p>
