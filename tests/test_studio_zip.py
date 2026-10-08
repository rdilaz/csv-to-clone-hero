import hashlib
import io
import random
import zipfile
from csv_to_clonehero.exporter import ExportSelection, ExportService, MANIFEST_FILENAME, _artifact_zip_compression
from csv_to_clonehero.models import DownloadInfo
from tests.conftest import make_chart


def test_large_audio_package_avoids_recompression_and_retains_verified_notes(config, tmp_path):
    payload = io.BytesIO()
    audio = random.Random(192).randbytes(512 * 1024)
    with zipfile.ZipFile(payload, 'w', zipfile.ZIP_STORED) as package:
        package.writestr('song.ini', '[song]\nname=Song\nartist=Artist\n')
        package.writestr('notes.chart', '[Song]\n{\nResolution=192\n}\n[ExpertSingle]\n{\n0 = N 0 0\n}\n')
        package.writestr('guitar.ogg', audio)
    data = payload.getvalue()
    chart = make_chart('Song', 'Artist', md5=hashlib.md5(data).hexdigest())
    class Source:
        name = 'test'
        supports_direct_download = True
        download_hash_verified = True
        def get_download_info(self, value):
            return DownloadInfo('test://chart', md5=value.md5, size_bytes=len(data))
        def download_chart(self, value, destination):
            destination.write_bytes(data)
            return destination
    service = ExportService(config, [Source()])
    try:
        result = service.export_zip([ExportSelection(chart, score=95, eligibility='RECOMMENDED')], tmp_path / 'charts.zip')
        assert result.success and result.published_count == 1
        assert result.published[0].playable_note_count == 1
        with zipfile.ZipFile(tmp_path / 'charts.zip') as archive:
            name = next(n for n in archive.namelist() if n.endswith('.sng'))
            assert archive.getinfo(name).compress_type == zipfile.ZIP_STORED
            assert archive.read(name) == data
            assert archive.getinfo(MANIFEST_FILENAME).compress_type == zipfile.ZIP_DEFLATED
            assert archive.testzip() is None
    finally:
        service.close()


def test_compressible_or_small_packages_keep_deflate(tmp_path):
    path = tmp_path / 'chart.sng'
    path.write_bytes(b'notes ' * 100000)
    assert _artifact_zip_compression(path, path.stat().st_size) == zipfile.ZIP_DEFLATED
    path.write_bytes(b'small')
    assert _artifact_zip_compression(path, 5) == zipfile.ZIP_DEFLATED
