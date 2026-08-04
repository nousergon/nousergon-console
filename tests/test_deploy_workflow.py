from pathlib import Path


DEPLOY_WORKFLOW = Path(__file__).resolve().parent.parent / ".github/workflows/deploy.yml"


def test_ssm_checkout_uses_literal_repo_path_without_nested_shell_variables():
    """The SSM shell must receive an executable checkout path, not ``$REPO``.

    A nested-shell escape converted the variable into the literal directory
    ``$REPO`` in production, so the clone failed before the deploy script ran.
    """
    workflow = DEPLOY_WORKFLOW.read_text()

    assert "CMD_BODY=" in workflow
    assert "cd /home/ec2-user/nousergon-console" in workflow
    assert "git clone https://github.com/nousergon/nousergon-console.git /home/ec2-user/nousergon-console" in workflow
    assert "REPO=/home/ec2-user/nousergon-console" not in workflow
