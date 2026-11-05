import run


def test_missing_arguments_exit_code(capsys):
    assert run.main(["run.py"]) == 2
    assert "usage" in capsys.readouterr().out


def test_unknown_role_exit_code(capsys):
    assert run.main(["run.py", "bogus", "1"]) == 2
