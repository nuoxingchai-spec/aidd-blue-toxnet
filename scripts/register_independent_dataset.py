"""Operator-only local registration; never grants rights or blind-test status."""
import argparse
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend.validation_service import (Registration,ValidationService,atomic_json,
                                       file_sha,load_json,trusted_file)


def validate_registration_binding(data_root, registration, *, seal_bundle=None):
    """Bind a registry entry to one strict, label-blind source and seal.

    This does not authorize label access, infer data independence or inspect
    the private label file. A bundle may be supplied only after verification
    by ``load_sealed_dataset(..., allow_labels=False)`` in the caller.
    """
    from backend.independent_validation_data import (CandidateMetadata, CandidateRow,
        IndependentProtocol, contract_dict, load_sealed_dataset, _json_loads)
    from backend.dataset_overlap_audit import object_sha256

    reg = registration if isinstance(registration, Registration) else Registration.model_validate(registration)
    root = Path(data_root).resolve()
    metadata = contract_dict(load_json(trusted_file(root, reg.metadataFile, expected=reg.metadataSha256)), CandidateMetadata)
    protocol = contract_dict(load_json(trusted_file(root, reg.protocolFile, expected=reg.protocolSha256)), IndependentProtocol)
    rows_file = trusted_file(root, reg.rowsFile, expected=reg.rowsSha256)
    rows = []
    with rows_file.open(encoding='utf-8') as stream:
        for line in stream:
            if line.strip():
                rows.append(contract_dict(_json_loads(line), CandidateRow))
            if len(rows) > 50000:
                raise ValueError('Registered label-blind member budget exceeded')
    if (metadata['datasetId'] != reg.id or metadata['datasetVersion'] != reg.version
            or metadata['rowsSha256'] != object_sha256(rows)
            or protocol['candidateMetadataSha256'] != object_sha256(metadata)
            or protocol['role'] != metadata['role']):
        raise ValueError('Registered dataset identity, source rows or protocol metadata differ')
    if bool(reg.sealFile) != bool(reg.sealSha256):
        raise ValueError('Seal file and its pinned SHA256 must be supplied together')
    if reg.sealFile is None:
        if seal_bundle is not None:
            raise ValueError('An unsealed registry entry cannot acquire a caller-supplied seal')
        return None
    if seal_bundle is None:
        seal_bundle = load_sealed_dataset(trusted_file(root, reg.sealFile, expected=reg.sealSha256),
                                         reg.sealSha256, purpose='evaluate', allow_labels=False)
    manifest = seal_bundle['manifest']
    if (seal_bundle.get('manifestSha256') != reg.sealSha256 or seal_bundle.get('labels') is not None
            or any('labels' in row for row in seal_bundle['rows'])
            or manifest.get('role') != 'independent-evaluation'
            or manifest.get('datasetId') != reg.id or manifest.get('datasetVersion') != reg.version
            or object_sha256(seal_bundle['metadata']) != object_sha256(metadata)
            or object_sha256(seal_bundle['protocol']) != object_sha256(protocol)
            or manifest.get('candidateMetadataSha256') != object_sha256(metadata)
            or manifest.get('frozenProtocolSha256') != object_sha256(protocol)
            or seal_bundle['metadata'].get('rowsSha256') != object_sha256(rows)):
        raise ValueError('Registered dataset, source rows and protocol do not match the verified seal')
    return seal_bundle


def register(data_root, *, identifier, name, version, metadata, rows, protocol, seal=None, seal_sha=None):
    root=Path(data_root).resolve()
    existing=root/'registry.json'
    # Every supplied path is relative to a pre-existing, protected private root.
    meta_path=trusted_file(root,metadata)
    row_path=trusted_file(root,rows)
    proto_path=trusted_file(root,protocol)
    from backend.independent_validation_data import CandidateMetadata,IndependentProtocol
    meta=CandidateMetadata.model_validate(load_json(meta_path))
    IndependentProtocol.model_validate(load_json(proto_path))
    if meta.datasetId != identifier or meta.datasetVersion != version:
        raise ValueError('Dataset ID/version differ from the reviewed candidate metadata')
    row=Registration(id=identifier,name=name,version=version,metadataFile=metadata,rowsFile=rows,
                     protocolFile=protocol,metadataSha256=file_sha(meta_path),rowsSha256=file_sha(row_path),
                     protocolSha256=file_sha(proto_path),sealFile=seal,sealSha256=seal_sha)
    validate_registration_binding(root, row)
    service=ValidationService(data_root=root)
    # Single exclusive operator registration lock; API has no write endpoint.
    root.mkdir(parents=True,exist_ok=True)
    lock=root/'.registry.lock'
    handle=lock.open('x',encoding='utf-8')
    try:
        previous=service._registrations()
        if identifier in previous or identifier in {'historical-reference-inventory','fda-dilirank2','chembl-assay-candidate'}:
            raise ValueError('Dataset registration is immutable; use a new audited dataset version/ID')
        catalog={'schema':'aidd.validation.registry.v1','datasets':[v.model_dump() for v in previous.values()]+[row.model_dump()]}
        atomic_json(existing,catalog)
    finally:
        handle.close()
        lock.unlink()
    return {'id':identifier,'version':version,'registered':True,'qualification':'pending-current-audit','sealed':seal is not None}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root',required=True)
    parser.add_argument('--dataset-id',required=True)
    parser.add_argument('--name',required=True)
    parser.add_argument('--version',required=True)
    parser.add_argument('--metadata',required=True,help='Relative metadata JSON within data root')
    parser.add_argument('--rows',required=True,help='Relative label-blind rows JSONL within data root')
    parser.add_argument('--protocol',required=True,help='Relative predeclared protocol JSON within data root')
    parser.add_argument('--sealed-manifest',default=None)
    parser.add_argument('--manifest-sha256',default=None)
    args=parser.parse_args()
    result=register(args.data_root,identifier=args.dataset_id,name=args.name,version=args.version,
                    metadata=args.metadata,rows=args.rows,protocol=args.protocol,
                    seal=args.sealed_manifest,seal_sha=args.manifest_sha256)
    import json
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__': main()
