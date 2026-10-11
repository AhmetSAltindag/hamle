WATReadOnly — authorized historical cloud reads

API:
  reader = WATReadOnly(GoogleSecretManager(), claim_refresh=existing_durable_claim)
  manifest = reader.manifest(task_id, comment_id, producer_id)
  data = reader.download(task_id, comment_id, attachment_id,
                         expected_sha256, expected_size, producer_id)
  guide = reader.guide_metadata()
  pdf_bytes = reader.download_guide()  # exact approved hash only
  reader.close()

Constructor makes no network call. The optional claim_refresh callback uses the
existing basecamp_cloud_auth permanent refresh-claim pathway. Without a callback,
an unexpired credential works; expired credentials hold safely. Do not add an
independent refresher. Secrets are accessed only through the supplied manager.

Allowed account5898865/project40694338/tasks10309337809,10064527849 and the newly
user-provided Samet reference10317695863. Expected producer is mandatory and must
be Berkay47658175 or Samet50486696. Runtime identity is verified by profile GET;
task/project/comment/parent/creator/status/visibility and exact native attachment
ID are checked on every manifest/download. Completed historical tasks may be read;
completion does not imply final approval. Native file names alone are never keys.

PNG/JPEG maximum16MiB. MP4 maximum64MiB only for Samet on selected task10317695863.
Expected full hash and size are mandatory. Size, full hash and type signature
are checked. Signature is not a media decoding/quality acceptance. Downstream
inspection still needs image/video decoding and brief/brand evidence. No ZIP,
PSD, arbitrary PDF, arbitrary task/project or arbitrary URL download API exists.

The only PDF exception is guide upload10392828700, exactly9910859bytes and approved
SHA2566be9fc432313ed5fd6d7741d52e83e584dddd26b8802238a01aee8b05ca3881c.
This does not authorize every file in the parent vault.

Every Basecamp business request is GET. At most one native blob302 redirect is
followed to exact storage.basecamp.com; Authorization is removed. No automatic
retry. Signed URLs/SGIDs/token values do not appear in returned manifests/errors.
OAuth POST/secret rotation can occur only inside the already approved fenced
auth helper. No task/comment/mail writer or cloud resource mutator is present.
Existing internal provider's2MiB limit is unchanged.

Tests: python -m unittest -q test_v11_wat_readonly
Tests block real sockets and fake all provider responses; no live writes or model
calls. The exact two Ekim case IDs/hashes are recorded under
outputs/wat-pilot-20261010/real-output-shadow/existing-file-revalidation.json.
