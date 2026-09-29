#!/usr/bin/env python3

from __future__ import print_function

import abc
import argparse
import functools
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import yaml

log = logging.getLogger(__name__)

WORKSPACES_FILE = "/home/daniel/dotfiles/config/files/workspace/workspaces.yaml"


def base_arg_parser():
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--mode", choices=["bash", "zsh"], default="bash")

    subparsers = parser.add_subparsers(
        title="subcommands",
        description="valid subcommands",
        help="additional help",
    )

    autocomplete_subparser = subparsers.add_parser("autocomplete_options")
    autocomplete_subparser.add_argument("forest", nargs="?", default=None)
    autocomplete_subparser.add_argument("--all", nargs="*", default=None)
    autocomplete_subparser.set_defaults(func=autocomplete_options)

    return parser, subparsers


def get_worktrees_from_dir(directory):
    cmd_out = subprocess.check_output(
        ["git", "-C", directory, "worktree", "list", "--porcelain"],
        encoding="utf-8",
    )
    log.debug(cmd_out)
    for line in cmd_out.splitlines():
        if line.startswith("worktree"):
            yield line.split()[1]


def is_subdir(_child, _parent):
    child = Path(_child)
    parent = Path(_parent)
    return child == parent or parent in child.parents


class Action(metaclass=abc.ABCMeta):
    action_instances = []

    @classmethod
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        log.debug(f"Instantiating action: {cls}")
        instance = cls()
        log.debug(f"Instantiated action: {instance}")
        Action.action_instances.append(instance)

    @classmethod
    def action_names(_):
        return [a.__action_name__ for a in Action.action_instances]

    @classmethod
    def action_suggestions(cls, mode):
        if mode == "zsh":
            return [f"{a.__action_name__}:{a.__action_description__}" for a in Action.action_instances]
        return cls.action_names()

    @classmethod
    def add_action_parsers(_, subparsers):
        for action in Action.action_instances:
            subparser = subparsers.add_parser(action.__action_name__)
            subparser.set_defaults(
                action_name=action.__action_name__, func=action.do
            )
            action.parse_args(subparser)

    def get_action(action_name):
        log.debug(f"Searching for Action: {action_name}")
        action = next(
            (
                a
                for a in Action.action_instances
                if a.__action_name__ == action_name
            ),
            None,
        )
        if not action:
            raise RuntimeError(f"Action not found: {action_name}")

        log.debug(f"Found action for [{action_name}]: {action}")
        return action

    @abc.abstractmethod
    def parse_args(self, subparser):
        raise NotImplementedError()

    @abc.abstractmethod
    def autocomplete(self, argv):
        raise NotImplementedError()

    @abc.abstractmethod
    def do(self, args):
        raise NotImplementedError()


class CheckoutAction(Action):
    __action_name__ = "checkout"
    __action_description__ = "checkout an existing Git branch into a new workspace"

    def parse_args(self, subparser):
        subparser.add_argument("forest")
        subparser.add_argument("branch")
        subparser.add_argument("worktree", default=None, nargs="?")

    def autocomplete(self, argv):
        forest_names = [f.name for f in load_forests()]
        if not argv or (len(argv) == 1 and argv[0] not in forest_names):
            return forest_names
        else:
            # TODO: List git branches
            return load_forest(argv[0]).worktree_names()

    def do(self, args):
        if not args.worktree:
            args.worktree = args.branch

        forest = load_forest(args.forest)
        new_tree = forest.add_worktree(args.worktree)
        return f"""(
    set -euxo pipefail;
    cd {forest.root_checkout_dir()};
    echo 'Creating new worktree: {args.worktree} by checking out: {args.branch}';
    git worktree add --detach {new_tree.root()};
    cd {new_tree.root()};
    git fetch origin {args.branch};
    git checkout {args.branch};
    git branch --set-upstream-to origin/master;
)
workspace source {args.forest} {args.worktree};
"""


class CreateAction(Action):
    __action_name__ = "create"
    __action_description__ = "create a new Git branch in a new workspace"

    def parse_args(self, subparser):
        subparser.add_argument("forest")
        subparser.add_argument("source_worktree")
        subparser.add_argument("branch")
        subparser.add_argument("worktree", default=None, nargs="?")

    def autocomplete(self, argv):
        forest_names = [f.name for f in load_forests()]
        if not argv or (len(argv) == 1 and argv[0] not in forest_names):
            return forest_names
        elif len(argv) == 2:
            return load_forest(argv[0]).worktree_names()
        else:
            # TODO: List git branches
            return load_forest(argv[0]).worktree_names()

    def do(self, args):
        if not args.worktree:
            args.worktree = args.branch

        forest = load_forest(args.forest)
        source_tree = forest.worktree(args.source_worktree)
        new_tree = forest.add_worktree(args.worktree)
        return f"""(
    set -euxo pipefail;
    cd {source_tree.root()};
    echo "Creating new worktree: {args.worktree} from existing: {source_tree.name}";
    git worktree add --detach {new_tree.root()};
    cd {new_tree.root()};
    git checkout -B {args.branch} --track origin/master;
    if [ -f {source_tree.root()}/compile_commands.json ]; then
      cp {source_tree.root()}/compile_commands.json {new_tree.root()};
    else
      echo "No compile_commands.json found in source worktree"
    fi
)
workspace source {args.forest} {args.worktree};
"""


class SourceAction(Action):
    __action_name__ = "source"
    __action_description__ = "cd to and begin work on an existing workspace"

    def parse_args(self, subparser):
        subparser.add_argument("forest", nargs="?")
        subparser.add_argument("tree", nargs="?")

    def autocomplete(self, argv):
        forest_names = [f.name for f in load_forests()]
        if not argv or (len(argv) == 1 and (argv[0] not in forest_names)):
            return forest_names
        else:
            return load_forest(argv[0]).worktree_names()

    def _find_worktree_from_cwd(self, cwd):
        for worktree in get_worktrees_from_dir(cwd):
            if is_subdir(cwd, worktree):
                log.debug("YES: " + str(worktree))
                return worktree
            else:
                log.debug("NO:  " + str(worktree))

    def _get_forest_and_tree_from_cwd(self):
        cwd = Path(os.getcwd())
        worktree_path = self._find_worktree_from_cwd(cwd)
        if not worktree_path:
            return None

        forests = load_forests()
        forest = next((f for f in forests if is_subdir(cwd, f.root_)), None)
        if not forest:
            return None

        wt_path = Path(worktree_path)
        forest_path = Path(forest.root_)
        worktree_name = str(
            Path(os.path.join(*wt_path.parts[len(forest_path.parts) :]))
        )

        return forest, forest.worktree(worktree_name)

    def do(self, args):
        cmd = []
        if not any((args.forest, args.tree)):
            forest, tree = self._get_forest_and_tree_from_cwd()
            cmd += [
                f"echo 'Auto-detected forest: {forest.name}';",
                f"echo 'Auto-detected worktree: {tree.name}';",
            ]
        else:
            forest = load_forest(args.forest)
            tree = forest.worktree(args.tree)

        cmd += [
            f"echo 'Activating workspace: {tree.name}';",
            f"builtin cd {tree.root()};",
            "tmux rename-window \"$(git symbolic-ref HEAD | sed 's#^refs/heads/##')\" | true;",
            ]
        if forest.source_script():
            cmd += [
                f"echo 'Sourcing workspace [{tree.name}]: {forest.source_script()}';",
                f"source '{tree.source_file()}';",
            ]
        cmd += [
            "echo -n 'Adding ssh-agent... '; ssh_agent_canonicalize",
        ]
        return "\n".join(cmd)


class ChdirAction(Action):
    __action_name__ = "cd"
    __action_description__ = "cd to an existing workspace"

    def parse_args(self, subparser):
        subparser.add_argument("forest")
        subparser.add_argument("tree")

    def autocomplete(self, argv):
        forest_names = [f.name for f in load_forests()]
        if not argv or (len(argv) == 1 and (argv[0] not in forest_names)):
            return forest_names
        else:
            return load_forest(argv[0]).worktree_names()

    def do(self, args):
        forest = load_forest(args.forest)
        tree = forest.worktree(args.tree)
        return f"builtin cd {tree.root()};"


class ListAction(Action):
    __action_name__ = "list"
    __action_description__ = "list all available workspaces"

    def parse_args(self, subparser):
        subparser.add_argument("forest")
        subparser.add_argument("query", nargs="?", default="")
        subparser.add_argument("--paths", action="store_true")

    def autocomplete(self, argv):
        forest_names = [f.name for f in load_forests()]
        if not argv or (len(argv) == 1 and (argv[0] not in forest_names)):
            return forest_names
        else:
            return load_forest(argv[0]).worktree_names()

    def do(self, args):
        forest = load_forest(args.forest)
        pattern = re.compile(args.query)
        matches = [
            n for n in forest.worktree_names()
            if n.startswith(args.query) or pattern.match(n)
        ]
        if args.paths:
            matches_str = '\n'.join([forest.worktree(m).root() for m in matches])
        else:
            matches_str = '\n'.join(matches)
        return f"echo '{matches_str}'"


class WorkTree(object):
    def __init__(self, parent, name):
        self.parent = parent
        self.name = name

    def root(self):
        return os.path.join(self.parent.root_, self.name)

    def source_file(self):
        return os.path.join(self.root(), self.parent.source_script())


class WorkForest(object):
    def __init__(self, body):
        self.name = body["name"]
        self.root_ = body["root"]
        self.root_checkout = body["root_checkout"]
        self.source_ = body.setdefault("source", None)

        self.worktrees_ = dict()

        self._find_worktrees()

    @functools.lru_cache()
    def _find_worktrees(self):
        for worktree in get_worktrees_from_dir(self.root_checkout_dir()):
            tree_name = os.path.relpath(worktree, self.root_)
            self.add_worktree(tree_name)

    def add_worktree(self, name):
        log.debug(f"Adding worktree: {name}")
        self.worktrees_[name] = WorkTree(self, name)
        return self.worktrees_[name]

    def worktree_names(self):
        return self.worktrees_.keys()

    def worktree(self, worktree):
        return self.worktrees_[worktree]

    def root_checkout_dir(self):
        return os.path.join(self.root_, self.root_checkout)

    def source_script(self):
        return self.source_


@functools.lru_cache()
def load_forests():
    ws_file = yaml.load(open(WORKSPACES_FILE, "r"), Loader=yaml.SafeLoader)
    forests = []
    for forest_description in ws_file["workforest"]:
        forest = WorkForest(forest_description)
        forests.append(forest)
    return forests


def load_forest(forest_name):
    for forest in load_forests():
        if forest.name == forest_name:
            return forest
    else:
        log.error(f"Forest not found: {forest_name}")


def autocomplete_options(args):
    log.debug(sys.argv)
    log.debug(f"All comp words: {args.all}")

    comp = [a.strip() for a in args.all]
    # Strip off leading 'workspace' arg
    if comp[0] == "workspace":
        comp = comp[:1]
    log.debug(f"stripped: {comp}")

    options = []
    if not comp or comp[0] not in Action.action_names():
        log.debug(f"stripped: {comp}")
        options = Action.action_suggestions(args.mode)
        options_str = ",".join(options)
    else:
        options = Action.get_action(comp[0]).autocomplete(comp[1:])
        options_str = " ".join(options)

    log.debug(f"suggestions: {options_str}")
    return options_str


def main(argv=None):
    verbose = bool(os.getenv("WORKSPACE_VERBOSE", False))
    logging.basicConfig(level=(logging.DEBUG if verbose else logging.WARNING))

    parser, subparsers = base_arg_parser()
    Action.add_action_parsers(subparsers)
    args = parser.parse_args(argv)

    print(args.func(args))


if __name__ == "__main__":
    main()
