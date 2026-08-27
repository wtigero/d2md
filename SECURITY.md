# Security Policy

`d2md` is a local document parser. Treat every document supplied to it as
untrusted input, including PDFs, Office files, archives, images, and files
from shared folders.

## Supported versions

| Version | Security support |
| --- | --- |
| `0.1.x` | Supported |
| `<0.1.0` | Not supported |

Security fixes are evaluated against the latest supported release line. A
development checkout may change before publication.

## Reporting a vulnerability

For the public repository, report suspected vulnerabilities through GitHub's
private vulnerability reporting feature: open the repository's **Security and
quality** tab and choose **Report a vulnerability**. The release process
requires this route to be enabled and verified before a public release is
announced.

Include the affected version or commit, operating system, input type,
reproduction steps, and any minimized document that can be shared safely.
Redact confidential document contents and credentials.

If **Report a vulnerability** is missing, do not put sensitive details in a
public issue. Open a nonsensitive issue titled `Private security reporting is
unavailable` that asks the maintainers to restore the private route, without
including vulnerability details, proof of concept, affected samples, or
credentials.

Reports are kept private while they are assessed. Maintainers may request
additional reproduction details, evaluate impact and affected versions, and
coordinate a fix or advisory with the reporter. Response times are not
guaranteed.

## Scope and threat boundary

The security boundary includes malformed or hostile documents, archive
expansion, symbolic links, path traversal, resource exhaustion, terminal
output, and optional OCR or Docling components used during local conversion.
The CLI applies documented resource and path protections, but those controls
are not a full sandbox.

`d2md` runs with the permissions of the invoking user and relies on native
and third-party parsers. MarkItDown and Docling full-string backends run in
reusable, independent Python workers. Each worker enforces the output ceiling
before the long-lived caller accepts or materializes the result; failures,
timeouts, and protocol errors discard the worker, while successful workers stay
warm. This process boundary is not a whole-host sandbox or a hard RAM/GPU
boundary, and it does not protect a host that is already compromised or
guarantee that other software is safe. Run untrusted conversions with the
least privileges and filesystem access practical, keep resource limits
enabled, and review optional model downloads before first use.

The default MarkItDown and Docling backend job deadline is 30 minutes (1,800
seconds). Only the explicit trusted-input `--unsafe-unlimited` mode makes this
deadline unlimited; it does not remove the need for OS, container, or job-level
controls.

For shared folders, copy inputs into a directory that other processes cannot
mutate before starting a recursive conversion. On Windows, also place outputs
under a parent directory controlled by the invoking user. These constraints
avoid portable directory-enumeration and output-publication races that cannot
be eliminated with path-only filesystem APIs.

Input, archive, page, and pixel ceilings are per file rather than aggregate
compute budgets for a directory run. Split attacker-controlled trees into
small batches and apply operating-system, container, or job-level time and
memory limits when the whole run needs a hard resource boundary.

Format-specific preflight uses verified content in addition to the filename.
ZIP-based Office documents and EPUB files receive archive limits even when a
caller supplies a misleading name, and their package markers must match the
declared document family. Generic or masquerading ZIP archives are rejected,
and recursive ZIP conversion is disabled in the fallback parser.
Content-detected PDFs and supported images receive their page and pixel checks
before the matching fallback parser is enabled.

General usage questions and ordinary conversion failures belong in the normal
project support channels, not in private vulnerability reports.

Do not publicly disclose a vulnerability, proof of concept, affected sample,
or report contents before maintainers have assessed it and coordinated any
disclosure.
