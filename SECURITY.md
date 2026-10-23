# Security policy

Report suspected malicious wheels, signing-key exposure, unsafe build recipes,
or repository compromise privately to the repository owner. Do not publish a
proof-of-concept exploit or upload a replacement wheel to the public index.

Every accepted native wheel must have a reviewed source, reproducible recipe,
license, source digest, SBOM, ABI/API/Python compatibility metadata, verified
SHA-256, passing import test, and a valid Ed25519 manifest signature. The
consumer must pin the corresponding public key independently of the repository
content.

GitHub Actions on untrusted pull requests run without signing secrets and have
read-only repository permissions. Publication must use a protected environment
and a separately approved release workflow. Never enable runtime native-wheel
installation in the Google Play profile.
