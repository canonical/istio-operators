import pytest

from charm import (
    is_qualified_name,
    valid_annotations,
    validate_annotation_key,
    validate_annotation_value,
)


@pytest.mark.parametrize(
    "value, expected_output",
    (
        ("something/invalid/api.io", False),
        ("/something-invalid", False),
        ("-invalid.com/fail", False),
        ("invalid..com/fail", False),
        ("invalid-.com/fail", False),
        ("valid.com/pass", True),
        ("valid.io/pass", True),
        ("valid.domain/pass", True),
        ("validname", True),
        ("valid-name", True),
        ("valid.name", True),
        (".invalidname", False),
        ("-invalidname", False),
        ("invalid@name", False),
        ("invalidname.", False),
        ("invalid/", False),
        ("/", False),
        (
            "eweizhf4i1ukpfot1jz14l3qzhjvsesa2e8egmjz4ushqblsei4f2bpgb287zteptxrk1u6rvjbjhugr8db7/",
            False,
        ),
    ),
)
def test_is_qualified_name(value, expected_output):
    """Ensure the is_qualified_name returns correct bool."""
    assert is_qualified_name(value) == expected_output


@pytest.mark.parametrize(
    "annotations, expected_output",
    (
        ({"service.beta.kubernetes.io/mycloud-load-balancer-internal": "true"}, True),
        ({"-service.beta.kubernetes.io/mycloud-load-balancer-internal": "true"}, False),
        ({"key1": ""}, False),
        ({"": ""}, False),
    ),
)
def test_valid_annotations(annotations, expected_output):
    """Ensure the valid_annotations returns correct bool."""
    assert valid_annotations(annotations) == expected_output


@pytest.mark.parametrize(
    "value, expected_output",
    (
        ("valid-value", True),
        ("validvalue1", True),
        ("valid.value", True),
        ("valid_value", True),
        ("invalid value", False),
        ("invalid!value", False),
    ),
)
def test_validate_annotation_value(value, expected_output):
    """Ensure the validate_annotation_value returns correct bool."""
    assert validate_annotation_value(value) == expected_output


@pytest.mark.parametrize(
    "key, expected_output",
    (
        (
            "ej8fep89xu3tdoj4ygn0lpfxl6r8bldf4ttleuocgsn8b772kxzkmwbsvma3sfzxc833c45j8us2bvg457hbnrp6l71dv68dktom8xki2sn4z3w4n7j5wbi8q80528ncalc2jizup3b8a9haa3racwu1dsq2m7zbfxkvgjx33d9xb7saeueirvjtk4n3tazg565ebihd7wr7uw9uw5skeu3yzxx4vqa5ivta1bkky61ot64xapmrhlbun61j83p0lb95at5me0vcsdjhrge55rwcjj9o9yf3rsquwkmdvfreg5d1w2n7mmhxgvsp",  # noqa
            False,
        ),
        ("kubernetes.io/something-invalid", False),
        ("k8s.io/something-invalid", False),
        ("service.beta.kubernetes.io/mycloud-load-balancer-internal", True),
    ),
)
def test_validate_annotation_key(key, expected_output):
    """Ensure the validate_annotation_key returns correct bool."""
    assert validate_annotation_key(key) == expected_output
