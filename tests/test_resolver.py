# Copyright 2015 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

import os
import time

import pytest
from twitter.common.contextutil import temporary_dir

from pex.common import safe_copy
from pex.crawler import Crawler
from pex.fetcher import Fetcher
from pex.package import EggPackage, SourcePackage
from pex.resolvable import ResolvableRequirement
from pex.resolver import Unsatisfiable, _ResolvableSet, resolve
from pex.resolver_options import ResolverOptionsBuilder
from pex.testing import make_sdist


def test_empty_resolve():
  empty_resolve = resolve([])
  assert empty_resolve == []

  with temporary_dir() as td:
    empty_resolve = resolve([], cache=td)
    assert empty_resolve == []


def test_simple_local_resolve():
  project_sdist = make_sdist(name='project')

  with temporary_dir() as td:
    safe_copy(project_sdist, os.path.join(td, os.path.basename(project_sdist)))
    fetchers = [Fetcher([td])]
    dists = resolve(['project'], fetchers=fetchers)
    assert len(dists) == 1


def test_diamond_local_resolve_cached():
  # This exercises the issue described here: https://github.com/pantsbuild/pex/issues/120
  project1_sdist = make_sdist(name='project1', install_reqs=['project2<1.0.0'])
  project2_sdist = make_sdist(name='project2')

  with temporary_dir() as dd:
    for sdist in (project1_sdist, project2_sdist):
      safe_copy(sdist, os.path.join(dd, os.path.basename(sdist)))
    fetchers = [Fetcher([dd])]
    with temporary_dir() as cd:
      dists = resolve(['project1', 'project2'], fetchers=fetchers, cache=cd, cache_ttl=1000)
      assert len(dists) == 2


def test_cached_dependency_pinned_unpinned_resolution_multi_run():
  # This exercises the issue described here: https://github.com/pantsbuild/pex/issues/178
  project1_0_0 = make_sdist(name='project', version='1.0.0')
  project1_1_0 = make_sdist(name='project', version='1.1.0')

  with temporary_dir() as td:
    for sdist in (project1_0_0, project1_1_0):
      safe_copy(sdist, os.path.join(td, os.path.basename(sdist)))
    fetchers = [Fetcher([td])]
    with temporary_dir() as cd:
      # First run, pinning 1.0.0 in the cache
      dists = resolve(['project', 'project==1.0.0'], fetchers=fetchers, cache=cd, cache_ttl=1000)
      assert len(dists) == 1
      assert dists[0].version == '1.0.0'
      # This simulates separate invocations of pex but allows us to keep the same tmp cache dir
      Crawler.reset_cache()
      # Second, run, the unbounded 'project' req will find the 1.0.0 in the cache. But should also
      # return SourcePackages found in td
      dists = resolve(['project', 'project==1.1.0'], fetchers=fetchers, cache=cd, cache_ttl=1000)
      assert len(dists) == 1
      assert dists[0].version == '1.1.0'
      # Third run, if exact resolvable and inexact resolvable, and cache_ttl is expired, exact
      # resolvable should pull from pypi as well since inexact will and the resulting
      # resolvable_set.merge() would fail.
      Crawler.reset_cache()
      time.sleep(1)
      dists = resolve(['project', 'project==1.1.0'], fetchers=fetchers, cache=cd,
                      cache_ttl=1)
      assert len(dists) == 1
      assert dists[0].version == '1.1.0'


def test_ambiguous_transitive_resolvable():
  # If an unbounded or larger bounded resolvable is resolved first, and a
  # transitive resolvable is resolved later in another round, Error(Ambiguous resolvable) can be
  # raised because foo pulls in foo-2.0.0 and bar->foo==1.0.0 pulls in foo-1.0.0.
  foo1_0 = make_sdist(name='foo', version='1.0.0')
  foo2_0 = make_sdist(name='foo', version='2.0.0')
  bar1_0 = make_sdist(name='bar', version='1.0.0', install_reqs=['foo==1.0.0'])
  with temporary_dir() as td:
    for sdist in (foo1_0, foo2_0, bar1_0):
      safe_copy(sdist, os.path.join(td, os.path.basename(sdist)))
    fetchers = [Fetcher([td])]
    with temporary_dir() as cd:
      dists = resolve(['foo', 'bar'], fetchers=fetchers, cache=cd,
                      cache_ttl=1000)
      assert len(dists) == 2
      assert dists[0].version == '1.0.0'


def test_resolve_prereleases():
  stable_dep = make_sdist(name='dep', version='2.0.0')
  prerelease_dep = make_sdist(name='dep', version='3.0.0rc3')

  with temporary_dir() as td:
    for sdist in (stable_dep, prerelease_dep):
      safe_copy(sdist, os.path.join(td, os.path.basename(sdist)))
    fetchers = [Fetcher([td])]

    def assert_resolve(expected_version, **resolve_kwargs):
      dists = resolve(['dep>=1,<4'], fetchers=fetchers, **resolve_kwargs)
      assert 1 == len(dists)
      dist = dists[0]
      assert expected_version == dist.version

    assert_resolve('2.0.0')
    assert_resolve('2.0.0', allow_prereleases=False)
    assert_resolve('3.0.0rc3', allow_prereleases=True)


def test_resolvable_set():
  builder = ResolverOptionsBuilder()
  rs = _ResolvableSet()
  rq = ResolvableRequirement.from_string('foo[ext]', builder)
  source_pkg = SourcePackage.from_href('foo-2.3.4.tar.gz')
  binary_pkg = EggPackage.from_href('Foo-2.3.4-py3.4.egg')

  rs.merge(rq, [source_pkg, binary_pkg])
  assert rs.get(source_pkg.name) == set([source_pkg, binary_pkg])
  assert rs.get(binary_pkg.name) == set([source_pkg, binary_pkg])
  assert rs.packages() == [(rq, set([source_pkg, binary_pkg]), None)]

  # test methods
  assert rs.extras('foo') == set(['ext'])
  assert rs.extras('Foo') == set(['ext'])

  # test filtering
  rs.merge(rq, [source_pkg])
  assert rs.get('foo') == set([source_pkg])
  assert rs.get('Foo') == set([source_pkg])

  with pytest.raises(Unsatisfiable):
    rs.merge(rq, [binary_pkg])


def test_resolvable_set_built():
  builder = ResolverOptionsBuilder()
  rs = _ResolvableSet()
  rq = ResolvableRequirement.from_string('foo', builder)
  source_pkg = SourcePackage.from_href('foo-2.3.4.tar.gz')
  binary_pkg = EggPackage.from_href('foo-2.3.4-py3.4.egg')

  rs.merge(rq, [source_pkg])
  assert rs.get('foo') == set([source_pkg])
  assert rs.packages() == [(rq, set([source_pkg]), None)]

  with pytest.raises(Unsatisfiable):
    rs.merge(rq, [binary_pkg])

  updated_rs = rs.replace_built({source_pkg: binary_pkg})
  updated_rs.merge(rq, [binary_pkg])
  assert updated_rs.get('foo') == set([binary_pkg])
  assert updated_rs.packages() == [(rq, set([binary_pkg]), None)]
