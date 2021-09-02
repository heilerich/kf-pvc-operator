#!/bin/sh
docker build . -t $(basename `pwd`):debug \
  && docker run --rm -it -v $HOME/.kube/config:/kubeconfig \
  -e KUBECONFIG=/kubeconfig \
  -e NAMESPACE_SELECTOR="app.kubernetes.io/part-of=kubeflow-profile" \
  -v $(pwd)/test_templates:/templates \
  $(basename `pwd`):debug
