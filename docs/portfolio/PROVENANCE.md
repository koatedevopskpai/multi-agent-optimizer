# Audit preservation and publication provenance

The original `feature/focused-delivery-recovery` branch remains unchanged at **6be9f33eb5e0de2f9c571ccb1231e0352d1cb971**. Its 145 commits beyond the public baseline and original evidence retain their identities. A verified private Git bundle records its complete history. Raw screenshots, backups and private execution state remain outside this publication branch.

This portfolio branch starts at public baseline **4b3653a8456531facdc5f570205ff7a5863fc1b6**, with a curated snapshot. It does not rewrite the audit branch. Original commit IDs in adapted historical documents identify private provenance, not necessarily publicly accessible GitHub commits.

## Publication changes

- Reviewer-focused introduction, business case, architecture, quality/evidence and demo guides.
- Portable home-directory lookup in helper scripts; generalized synthetic MFA fixture.
- Development dependencies explicitly include the pinned SDK required to collect mocked IAM bootstrap tests; no missing-dependency skip is used.
- Personal paths/account/resource identifiers removed from publication copies of historical docs, which are explicitly labelled as adapted references.
- Deploy-policy template includes the final reviewed lifecycle deletion and London-only network-interface inventory permissions. Publishing a template does not change live IAM.
- Twelve approved derivatives copied without byte changes; [approved-images.json](approved-images.json) records their hashes.
- Raw development evidence/log archives excluded; attributed summaries distinguish historical measurements from current reruns.
- Portable POSIX test runner uses native bash on Linux and WSL on Windows; unavailable execution environments are explicitly skipped. CI installs Terraform for the local built-in-provider regression tests.
- LF text checkout policy; binary images retain exact bytes.

Commit hashes identify Git history; SHA256 identifies artifact bytes. New publication commits do not change original history or evidence. Edited documents are new derivatives; unchanged images retain their approved hashes. The [release manifest](release-manifest.json) records source and publication hashes. The private audit bundle and detailed local review records are retained outside the public package.

AI coding/review tools assisted implementation, tests and evidence preparation. Koate owns the engineering decisions and demonstration. Synthetic scenarios and a controlled reference are distinct from customer production experience.

Preparation does not push, merge, change repository visibility, deploy cloud resources or publish anything.
